import asyncio
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from nexus.contracts.voice import VoiceSpeakCompleted, pack_voice_audio, unpack_voice_audio
from nexus.core.app import create_app
from nexus.core.config import VoiceConfig
from nexus.core.security.types import AccessDenied
from nexus.core.voice.providers import FakeAsrProvider, FakeTtsProvider
from nexus.core.voice.service import VoiceService
from nexus.voice.audio import AudioEngine
from nexus.voice.config import RuntimeConfig
from test_voice import config, ORIGIN


@pytest.mark.skipif(sys.platform != "win32", reason="Windows COM apartments")
def test_default_endpoints_reuse_portaudio_com_apartment():
    pytest.importorskip("sounddevice")  # PortAudio initializes COM before endpoint polling.
    from nexus.voice.devices import default_endpoint_ids
    values = default_endpoint_ids()
    assert values is None or len(values) == 2


class Sink:
    def __init__(self):
        self.messages = []
        self.audio = asyncio.Event()
        self.ended = asyncio.Event()

    async def send(self, data):
        self.messages.append(data)
        if isinstance(data, bytes):
            _, pcm, end = unpack_voice_audio(data)
            if pcm:
                self.audio.set()
            if end:
                self.ended.set()

    async def flush(self):
        self.messages.clear()


def test_abandoned_activation_expires_without_opening_asr(world):
    async def scenario():
        asr = FakeAsrProvider()
        service = VoiceService(world.db, VoiceConfig(enabled=True), world.owner_id,
                               asr=asr, tts=FakeTtsProvider())
        session = await service.prepare(world.owner, world.node_id, uuid4(), "button", uuid4(), {}, 60)
        session.started_at -= timedelta(seconds=31)
        task = asyncio.create_task(service.watch())
        try:
            async with asyncio.timeout(2):
                while session.state != "stopped":
                    await asyncio.sleep(0.05)
            assert not asr.sessions
            assert session.state == "stopped"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await service.close()
    asyncio.run(scenario())


async def prepared(world, service):
    session = await service.open_session(world.node, uuid4(), "button")
    session.framed_audio = True
    sink = Sink()
    await service.attach_control(session, sink)
    await service.attach_audio(session, sink)
    await service.begin_listening(session)
    return session, sink


def test_next_listen_does_not_bind_blank_input_to_previous_chat(world):
    async def scenario():
        service = VoiceService(world.db, VoiceConfig(enabled=True), world.owner_id,
                               asr=FakeAsrProvider(), tts=FakeTtsProvider())
        session = await service.open_session(world.node, uuid4(), "button")
        session.chat_turn_id = str(uuid4())
        await service.begin_listening(session)
        async with world.db.transaction(write=False) as conn:
            cursor = await conn.execute("SELECT turn_id,text FROM voice_utterances WHERE utterance_id=?",
                                        (str(session.utterance_id),))
            row = await cursor.fetchone()
        assert row["turn_id"] is None and row["text"] == ""
        session.media_stopped.set()
        await service.close()
    asyncio.run(scenario())


def test_streams_before_chat_finishes_and_waits_for_device_ack(world):
    async def scenario():
        release = asyncio.Event()
        async def stream(session, text):
            yield "第一句话。"
            await release.wait()
            yield "第二句话。"
        service = VoiceService(world.db, VoiceConfig(enabled=True), world.owner_id,
            asr=FakeAsrProvider(), tts=FakeTtsProvider(seconds=0.02), streamer=stream)
        session, sink = await prepared(world, service)
        await service.accept_final(session, "测试")
        async with asyncio.timeout(2):
            await sink.audio.wait()
        assert not release.is_set() and not session.response_task.done()
        release.set()
        async with asyncio.timeout(2):
            await sink.ended.wait()
        assert session.state == "speaking"
        def ack(generation):
            return VoiceSpeakCompleted(type="voice.speak.completed", message_id=uuid4(), sent_at=datetime.now(UTC),
                voice_session_id=session.voice_session_id, session_epoch=1,
                utterance_id=session.assistant_utterance_id, generation=generation, segments=2, total_ms=40)
        await service.playback_ack(session, ack(0))
        assert not session.playback_done.is_set()
        await service.playback_ack(session, ack(session.generation))
        async with asyncio.timeout(2):
            await session.response_task
        assert session.state == "listening"
        session.media_stopped.set()
        await service.close()
    asyncio.run(scenario())


def test_barge_in_cancels_original_stream_and_starts_fresh_listen(world):
    async def scenario():
        cancelled = asyncio.Event()
        async def stream(session, text):
            try:
                yield "正在回答。"
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        service = VoiceService(world.db, VoiceConfig(enabled=True), world.owner_id,
            asr=FakeAsrProvider(), tts=FakeTtsProvider(seconds=0.02), streamer=stream)
        session, sink = await prepared(world, service)
        old_user = session.utterance_id
        await service.accept_final(session, "你好")
        async with asyncio.timeout(2):
            await sink.audio.wait()
        generation = session.generation
        await service.barge_in(session)
        assert cancelled.is_set() and session.generation > generation
        assert session.state == "listening" and session.utterance_id != old_user
        session.media_stopped.set()
        await service.close()
    asyncio.run(scenario())


def test_real_provider_requires_owner_activation_and_unknown_quota_is_retained(world):
    async def scenario():
        cfg = VoiceConfig(enabled=True, provider="bailian", workspace_id="test", voice_id="test", daily_asr_seconds=60)
        service = VoiceService(world.db, cfg, world.owner_id, asr=FakeAsrProvider(), tts=FakeTtsProvider())
        with pytest.raises(AccessDenied, match="MODEL_CONSENT_REQUIRED"):
            await service.open_session(world.node, uuid4(), "button")
        with pytest.raises(AccessDenied, match="FORBIDDEN"):
            await service.prepare(world.node, world.node_id, uuid4(), "button", uuid4(), {}, 60)
        request = uuid4()
        session = await service.prepare(world.owner, world.node_id, request, "button", uuid4(), {"allow_model": True}, 60)
        assert await service.open_session(world.node, request, "button") is session
        await service.begin_listening(session)
        session.media_stopped.set()
        await service.stop(session, "user")
        assert session.calls[0]["status"] == "unknown" and session.calls[0]["reserved"] == 60
        second = await service.prepare(world.owner, world.node_id, uuid4(), "button", uuid4(), {}, 60)
        with pytest.raises(AccessDenied, match="BUDGET_EXCEEDED"):
            await service.begin_listening(second)
        second.media_stopped.set()
        await service.close()
    asyncio.run(scenario())


def test_owner_http_binds_conversation_and_outbound_consent(world):
    with TestClient(create_app(config(world)), base_url=ORIGIN) as client:
        assert client.post("/api/v1/voice/sessions", json={}).status_code == 403
        login = client.post("/api/v1/session", json={"token": world.owner_token.get_secret_value()}, headers={"origin": ORIGIN})
        headers = {"origin": ORIGIN, "x-csrf-token": login.json()["csrf_token"]}
        conversation = client.post("/api/v1/chat/conversations", json={"request_id": str(uuid4())}, headers=headers).json()["conversation"]
        value = {"request_id": str(uuid4()), "node_id": str(world.node_id),
                 "conversation_id": conversation["conversation_id"], "allow_audio_model": False, "allow_model": True}
        assert client.post("/api/v1/voice/sessions", json=value, headers=headers).status_code == 403
        value["allow_audio_model"] = True
        response = client.post("/api/v1/voice/sessions", json=value, headers=headers)
        assert response.status_code == 200
        identifier = response.json()["voice_session_id"]
        assert client.post("/api/v1/voice/sessions", json=value, headers=headers).json()["voice_session_id"] == identifier
        assert client.post(f"/api/v1/voice/sessions/{identifier}/stop", json={}, headers=headers).status_code == 200


def test_audio_packets_reject_stale_metadata_formats():
    assert unpack_voice_audio(pack_voice_audio(9, b"\x01\x02")) == (9, b"\x01\x02", False)
    assert unpack_voice_audio(pack_voice_audio(9, end=True)) == (9, b"", True)
    for value in [b"", b"NXV2", b"broken" * 4, pack_voice_audio(1) + b"x"]:
        with pytest.raises(ValueError):
            unpack_voice_audio(value)


def test_output_preserves_order_and_uses_actually_rendered_reference():
    engine = AudioEngine(RuntimeConfig(core_url="ws://127.0.0.1", node_id=uuid4()))
    engine._out.extend(b"abcdefgh")
    first, second, silence = bytearray(4), bytearray(4), bytearray(4)
    for output in (first, second, silence):
        engine._output_cb(output, 2, None, None)
    assert (first, second, silence) == (b"abcd", b"efgh", b"\x00" * 4)
    assert [engine._far.get_nowait() for _ in range(3)] == [bytes(first), bytes(second), bytes(silence)]


def test_proactive_speak_requires_independent_listen_grant_and_never_starts_asr(world):
    async def scenario():
        cfg = VoiceConfig(enabled=True, dnd_start_hour=0, dnd_end_hour=0)
        asr = FakeAsrProvider()
        service = VoiceService(world.db, cfg, world.owner_id, asr=asr, tts=FakeTtsProvider(seconds=0.02))
        session = await service.prepare(world.owner, world.node_id, uuid4(), "wake", uuid4(),
                                        {"proactive_speak": True, "proactive_listen": False}, 60)
        sink = Sink()
        await service.attach_control(session, sink)
        await service.attach_audio(session, sink)
        session.framed_audio = True
        with pytest.raises(AccessDenied, match="VOICE_NOT_GRANTED"):
            await service.announce(session, "提醒", listen=True)
        event_id = uuid4()
        assert await service.announce(session, "这是一次提醒。", event_id=event_id)
        async with asyncio.timeout(2):
            await sink.audio.wait()
        await service.barge_in(session)
        assert session.state == "activating" and session.voice_session_id not in asr.sessions
        assert not await service.announce(session, "这是一次提醒。", event_id=event_id)
        session.media_stopped.set()
        await service.close()
    asyncio.run(scenario())


def test_proactive_dnd_is_persisted_and_not_replayed(world, monkeypatch):
    async def scenario():
        service = VoiceService(world.db, VoiceConfig(enabled=True), world.owner_id,
                               asr=FakeAsrProvider(), tts=FakeTtsProvider())
        session = await service.prepare(world.owner, world.node_id, uuid4(), "wake", uuid4(),
                                        {"proactive_speak": True}, 60)
        sink = Sink()
        await service.attach_audio(session, sink)
        monkeypatch.setattr(service, "_dnd", lambda: True)
        identifier = uuid4()
        assert not await service.announce(session, "提醒", event_id=identifier)
        assert not session.calls and session.proactive[0]["status"] == "suppressed"
        monkeypatch.setattr(service, "_dnd", lambda: False)
        assert not await service.announce(session, "提醒", event_id=identifier)
        session.media_stopped.set()
        await service.close()
    asyncio.run(scenario())
