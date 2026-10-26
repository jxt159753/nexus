import asyncio
import json
import pytest

from websockets.asyncio.server import serve

from nexus.core.config import VoiceConfig
from nexus.core.voice import bailian


def config():
    return VoiceConfig(
        enabled=True, provider="bailian", workspace_id="ws-test",
        voice_id="voice-test", heartbeat_interval_seconds=1, heartbeat_timeout_seconds=5,
    )


def test_bailian_tts_protocol(monkeypatch):
    async def scenario():
        events = []

        async def handler(ws):
            run = json.loads(await ws.recv())
            events.append(("run", run["payload"]["model"], run["payload"]["parameters"]["voice"]))
            await ws.send(json.dumps({"header": {"event": "task-started"}, "payload": {}}))
            cont = json.loads(await ws.recv())
            events.append(("continue", cont["payload"]["input"]["text"]))
            finish = json.loads(await ws.recv())
            events.append(("finish", finish["header"]["action"]))
            await ws.send(b"\x00\x00")
            await asyncio.sleep(0.65)
            await ws.send(b"\x01\x00")
            await ws.send(json.dumps({"header": {"event": "task-finished"}, "payload": {}}))
            await ws.wait_closed()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(bailian, "_endpoint", lambda _: f"ws://127.0.0.1:{port}/")
            provider = bailian.BailianTtsProvider(config(), api_key="test")
            chunks = [chunk async for chunk in provider.synthesize("你好", "voice-test")]
        return events, chunks

    events, chunks = asyncio.run(scenario())
    assert ("run", "cosyvoice-v3.5-flash", "voice-test") in events
    assert ("continue", "你好") in events
    assert ("finish", "finish-task") in events
    assert chunks == [b"\x00\x00", b"\x01\x00"]


def test_bailian_asr_protocol(monkeypatch):
    async def scenario():
        partial, final = [], []
        done = asyncio.Event()

        async def handler(ws):
            run = json.loads(await ws.recv())
            assert run["payload"]["task"] == "asr"
            await ws.send(json.dumps({"header": {"event": "task-started"}, "payload": {}}))
            pcm = await ws.recv()
            assert pcm == b"\x00" * 320
            await ws.send(json.dumps({
                "header": {"event": "result-generated"},
                "payload": {"output": {"sentence": {
                    "text": "你好", "sentence_end": False, "sentence_id": 0, "heartbeat": False,
                }}},
            }))
            await ws.send(json.dumps({
                "header": {"event": "result-generated"},
                "payload": {"output": {"sentence": {
                    "text": "你好，NEXUS。", "sentence_end": True, "sentence_id": 0, "heartbeat": False,
                }}},
            }))
            await ws.recv()  # finish-task
            await ws.wait_closed()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(bailian, "_endpoint", lambda _: f"ws://127.0.0.1:{port}/")
            provider = bailian.BailianAsrProvider(config(), api_key="test")
            async def on_partial(text):
                partial.append(text)

            async def on_final(text):
                final.append(text)

            await provider.start(__import__("uuid").uuid4(), on_partial, on_final)
            await provider.send_audio(next(iter(provider.sessions)), b"\x00" * 320)
            async with asyncio.timeout(2):
                while not final:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            await provider.close()
        return partial, final

    partial, final = asyncio.run(scenario())
    assert partial == ["你好"]
    assert final == ["你好，NEXUS。"]


@pytest.mark.parametrize("pending_text", [True, False])
def test_tts_silence_waits_for_work_but_still_times_out_after_finish(monkeypatch, pending_text):
    async def scenario():
        sent = []
        original_wait = asyncio.wait_for
        async def short_audio_wait(awaitable, timeout):
            return await original_wait(awaitable, .02 if timeout == 45 else timeout)
        monkeypatch.setattr(bailian.asyncio, "wait_for", short_audio_wait)
        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps({"header":{"event":"task-started"}}))
            sent.append(json.loads(await ws.recv())["payload"]["input"]["text"])
            await ws.send(b"\x00\x00")
            if pending_text:
                sent.append(json.loads(await ws.recv())["payload"]["input"]["text"])
            assert json.loads(await ws.recv())["header"]["action"] == "finish-task"
            if pending_text:
                await ws.send(b"\x01\x00")
                await ws.send(json.dumps({"header":{"event":"task-finished"}}))
            await ws.wait_closed()
        async def texts():
            yield "已找到问题。"
            if pending_text:
                await asyncio.sleep(.09)  # Several receiver idle periods, no new speech.
                yield "修复完成。"
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(bailian, "_endpoint", lambda _: f"ws://127.0.0.1:{port}/")
            provider = bailian.BailianTtsProvider(config(), api_key="test")
            if pending_text:
                chunks = [chunk async for chunk in provider.stream(texts(), "voice-test")]
                assert chunks == [b"\x00\x00", b"\x01\x00"]
                assert sent == ["已找到问题。", "修复完成。"]
            else:
                with pytest.raises(bailian.ProviderUnavailable, match="TTS stream failed"):
                    _ = [chunk async for chunk in provider.stream(texts(), "voice-test")]
    asyncio.run(scenario())
