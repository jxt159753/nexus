import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from websockets.asyncio.server import serve

from nexus.contracts.voice import (
    VoiceBargeIn, VoiceListenStart, VoiceSessionStarted, VoiceSpeak, VoiceSpeakCompleted,
    VoiceStopped, VoiceWelcome, encode_voice_message, parse_voice_message, pack_voice_audio,
)
from nexus.voice import client
from nexus.voice.config import RuntimeConfig


class FakeAudio:
    def __init__(self):
        self.played = []
        self.opened = False
        self.output_opened = False
        self.closed = False
        self.flushed = 0
        self.aec_active = True
        self.speech_probability = 0.95
        self.near_rms = 0.05
        self.speech_ms = 140
        self.input_name = self.output_name = "fake"
        self.dropped_input = self.dropped_output = 0

    def prepare(self):
        pass

    def devices_changed(self):
        return False

    def drain(self):
        return True

    def open(self, with_output=True):
        self.opened = True

    def open_output(self):
        self.output_opened = True

    def read_uplink(self):
        return b"\x00" * 3200

    def play(self, pcm):
        self.played.append(pcm)

    def flush(self):
        self.flushed += 1

    def close(self):
        self.closed = True


def test_runtime_control_audio_flow(monkeypatch):
    fake = FakeAudio()
    monkeypatch.setattr(client, "AudioEngine", lambda config: fake)
    session_id, epoch, conversation_id = uuid4(), 1, uuid4()
    seen = {"hello": False, "activate": False, "uplink": 0, "audio_ws": False, "barge": False}

    async def handler(ws):
        if "/audio" in ws.request.path:
            seen["audio_ws"] = True
            seen["uplink"] += len(await ws.recv())
            await asyncio.sleep(0.28)
            await ws.send(pack_voice_audio(1, b"\x00" * 2204))
            await asyncio.sleep(2)
            await ws.wait_closed()
            return
        hello = parse_voice_message(await ws.recv())
        seen["hello"] = hello.type == "voice.hello"
        await ws.send(encode_voice_message(VoiceWelcome(
            type="voice.welcome", message_id=uuid4(), sent_at=datetime.now(UTC),
            connection_id=uuid4(), heartbeat_interval_seconds=1,
            heartbeat_timeout_seconds=5, policy_revision=1,
            max_session_seconds=60, max_final_chars=2000,
        )))
        activate = parse_voice_message(await ws.recv())
        seen["activate"] = activate.type == "voice.activate"
        await ws.send(encode_voice_message(VoiceSessionStarted(
            type="voice.session.started", message_id=uuid4(), sent_at=datetime.now(UTC),
            voice_session_id=session_id, session_epoch=epoch,
            conversation_id=conversation_id, mode="push_to_talk",
            expires_at=datetime.now(UTC).replace(year=2030),
        )))
        await ws.send(encode_voice_message(VoiceListenStart(
            type="voice.listen.start", message_id=uuid4(), sent_at=datetime.now(UTC),
            voice_session_id=session_id, session_epoch=epoch, utterance_id=uuid4(),
            max_seconds=60, language="zh",
        )))
        await asyncio.sleep(0.25)
        await ws.send(encode_voice_message(VoiceSpeak(
            type="voice.speak", message_id=uuid4(), sent_at=datetime.now(UTC),
            voice_session_id=session_id, session_epoch=epoch, utterance_id=uuid4(),
            generation=1, segment_index=0, text="你好", final_segment=True,
            not_after=datetime.now(UTC).replace(year=2030), voice="voice-test",
        )))
        async with asyncio.timeout(2):
            while True:
                message = parse_voice_message(await ws.recv())
                if isinstance(message, VoiceBargeIn):
                    seen["barge"] = True
                    break
        await ws.send(encode_voice_message(VoiceSpeakCompleted(
            type="voice.speak.completed", message_id=uuid4(), sent_at=datetime.now(UTC),
            voice_session_id=session_id, session_epoch=epoch, utterance_id=uuid4(),
            generation=1, segments=1, total_ms=100,
        )))
        await asyncio.sleep(0.1)
        await ws.send(encode_voice_message(VoiceStopped(
            type="voice.session.stopped", message_id=uuid4(), sent_at=datetime.now(UTC),
            voice_session_id=session_id, session_epoch=epoch, reason="test",
            execution_stopped=True,
        )))
        await ws.wait_closed()

    async def run():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            config = RuntimeConfig(core_url=f"ws://127.0.0.1:{port}", node_id=uuid4())
            runtime = client.VoiceRuntime(config, "test-token")
            async with asyncio.timeout(5):
                await runtime.run()

    asyncio.run(run())
    assert seen["hello"] and seen["activate"] and seen["audio_ws"]
    assert seen["uplink"] > 0
    assert seen["barge"] is True
    assert fake.opened and fake.closed
    # A packet from the interrupted generation is discarded even if it was
    # already in flight on the independent audio connection.
    assert fake.played == []
