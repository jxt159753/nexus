"""Wake standby privacy and repeated activation over real local WebSockets."""
import asyncio
import queue
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from websockets.asyncio.server import serve

from nexus.contracts.voice import (
    VoiceActivate, VoiceListenStart, VoiceSessionStarted, VoiceState, VoiceStop,
    VoiceStopped, VoiceWelcome, encode_voice_message, parse_voice_message,
)
from nexus.voice import client
from nexus.voice.audio import AudioUnavailable
from nexus.voice.config import RuntimeConfig
from test_voice_runtime import FakeAudio


def test_wake_requires_prepared_owner_scope():
    with pytest.raises(ValueError, match="owner-prepared"):
        RuntimeConfig(core_url="ws://127.0.0.1", node_id=uuid4(), wake_enabled=True)


def test_background_standby_only_uploads_after_wake_and_can_rearm(monkeypatch):
    class Audio(FakeAudio):
        def __init__(self):
            super().__init__()
            self.aec_active = False
            self.frames = queue.Queue()
            self.cues = 0

        def ready_cue(self):
            self.cues += 1

        def read_uplink(self):
            value = self.frames.get(timeout=3)
            if value is None:
                raise AudioUnavailable("closed")
            return value

        def close(self):
            super().close()
            self.frames.put(None)

    class Wake:
        def __init__(self, config):
            self.resets = 0
            self.received = []

        def reset(self):
            self.resets += 1

        def push(self, pcm):
            self.received.append(pcm[0])
            return "你好奈克斯" if pcm[0] == 3 else None

        def close(self):
            pass

    audio = Audio()
    monkeypatch.setattr(client, "AudioEngine", lambda config: audio)
    monkeypatch.setattr(client, "WakeDetector", Wake)

    async def scenario():
        session_id, request_id, conversation_id = uuid4(), uuid4(), uuid4()
        ready = asyncio.Event()
        seen = {"activations": 0, "audio": [], "stopped": False}
        connections = {}

        async def send(ws, kind, **values):
            await ws.send(encode_voice_message(kind(message_id=uuid4(), sent_at=datetime.now(UTC), **values)))

        async def handler(ws):
            if "/audio" in ws.request.path:
                async for pcm in ws:
                    seen["audio"].append(pcm[0])
                return
            connections["control"] = ws
            hello = parse_voice_message(await ws.recv())
            assert hello.activation_request_id == request_id
            await send(ws, VoiceWelcome, type="voice.welcome", connection_id=uuid4(),
                       heartbeat_interval_seconds=1, heartbeat_timeout_seconds=5,
                       policy_revision=1, max_session_seconds=60, max_final_chars=2000)
            await send(ws, VoiceSessionStarted, type="voice.session.started", voice_session_id=session_id,
                       session_epoch=1, conversation_id=conversation_id, mode="wake",
                       expires_at=datetime.now(UTC) + timedelta(minutes=1))
            async for raw in ws:
                message = parse_voice_message(raw)
                if isinstance(message, VoiceActivate):
                    seen["activations"] += 1
                    assert message.trigger == "wake" and message.request_id == request_id
                    await ready.wait()
                    await send(ws, VoiceListenStart, type="voice.listen.start", voice_session_id=session_id,
                               session_epoch=1, utterance_id=uuid4(), max_seconds=60, language="zh")
                elif isinstance(message, VoiceStopped):
                    seen["stopped"] = message.execution_stopped
                    assert message.capture_stopped_at and message.playback_stopped_at

        async def until(predicate):
            async with asyncio.timeout(3):
                while not predicate():
                    await asyncio.sleep(0.005)

        def frame(value):
            audio.frames.put(bytes([value]) * 1280)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            config = RuntimeConfig(core_url=f"ws://127.0.0.1:{port}", node_id=uuid4(), wake_enabled=True,
                                   request_id=request_id, conversation_id=conversation_id)
            runtime = client.VoiceRuntime(config, "local-test-token")
            task = asyncio.create_task(runtime.run())
            try:
                await until(lambda: runtime.state == "standby" and audio.opened)
                frame(1)
                frame(2)
                await until(lambda: len(runtime.wake.received) == 2)
                assert seen["activations"] == 0 and seen["audio"] == [] and audio.cues == 0
                frame(3)
                await until(lambda: seen["activations"] == 1)
                frame(4)
                await until(lambda: len(runtime.preroll) == 1)
                assert seen["audio"] == [] and audio.cues == 0
                ready.set()
                await until(lambda: runtime.listening.is_set())
                frame(5)
                await until(lambda: len(seen["audio"]) >= 2)
                assert seen["audio"] == [4, 5] and audio.cues == 1
                await send(connections["control"], VoiceState, type="voice.state", voice_session_id=session_id,
                           session_epoch=1, generation=1, state="activating", since=datetime.now(UTC))
                await until(lambda: runtime.state == "standby")
                assert runtime.wake.resets == 2 and not runtime.preroll
                frame(6)
                await until(lambda: runtime.wake.received[-1] == 6)
                assert seen["audio"] == [4, 5]
                frame(3)
                await until(lambda: seen["activations"] == 2 and audio.cues == 2)
                frame(7)
                await until(lambda: seen["audio"] == [4, 5, 7])
                await send(connections["control"], VoiceStop, type="voice.stop",
                           voice_session_id=session_id, session_epoch=1, reason="user")
                await asyncio.wait_for(task, 3)
                await until(lambda: seen["stopped"])
                assert audio.closed and seen["stopped"]
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
