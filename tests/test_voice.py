from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nexus.contracts.voice import (
    VoiceActivate, VoiceHello, VoiceListenStart, VoiceSessionStarted, VoiceSpeak,
    VoiceStopped, VoiceStop, VoiceStopRequest, VoiceWelcome, encode_voice_message, parse_voice_message,
)
from nexus.core.app import create_app
from nexus.core.config import CoreConfig, TrustedNode, VoiceConfig, VoiceNodePolicy


ORIGIN = "http://127.0.0.1:8000"


def config(world):
    return CoreConfig(
        data_dir=world.db.path.parent,
        owner_id=world.owner_id,
        local_http=True,
        origins=frozenset({ORIGIN}),
        heartbeat_interval_seconds=1,
        heartbeat_timeout_seconds=5,
        nodes={
            world.node_id: TrustedNode(
                voice=VoiceNodePolicy(enabled=True, features=frozenset({"capture", "playback"}))
            )
        },
        voice=VoiceConfig(
            enabled=True, provider="fake",
            heartbeat_interval_seconds=1, heartbeat_timeout_seconds=5,
        ),
    )


def auth(world):
    return {"authorization": "Bearer " + world.node_token.get_secret_value()}


def hello(world):
    return VoiceHello(
        type="voice.hello", message_id=uuid4(), sent_at=datetime.now(UTC),
        node_id=world.node_id, voice_version="test", boot_id=uuid4(),
        features=["capture", "playback", "wake", "streaming_asr", "streaming_tts", "aec"],
    )


def recv_until(ws, expected, limit=20):
    for _ in range(limit):
        message = parse_voice_message(ws.receive_text())
        if isinstance(message, expected):
            return message
    raise AssertionError(f"{expected.__name__} not received")


def test_voice_contract_roundtrip():
    message = VoiceHello(
        type="voice.hello", message_id=uuid4(), sent_at=datetime.now(UTC),
        node_id=uuid4(), voice_version="test", boot_id=uuid4(),
        features=["capture"],
    )
    raw = encode_voice_message(message)
    parsed = parse_voice_message(raw)
    assert isinstance(parsed, VoiceHello) and parsed.node_id == message.node_id


def test_control_and_audio_fake_flow(world):
    app = create_app(config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/voice", headers=auth(world)) as control:
            control.send_text(encode_voice_message(hello(world)))
            welcome = recv_until(control, VoiceWelcome)
            assert welcome.heartbeat_interval_seconds == 1

            control.send_text(encode_voice_message(VoiceActivate(
                type="voice.activate", message_id=uuid4(), sent_at=datetime.now(UTC),
                request_id=uuid4(), trigger="button", confidence=None,
            )))
            started = recv_until(control, VoiceSessionStarted)
            recv_until(control, VoiceListenStart)

            with client.websocket_connect(
                f"ws://127.0.0.1:8000/ws/v1/voice/audio?session_id={started.voice_session_id}&epoch={started.session_epoch}",
                headers=auth(world),
            ) as audio:
                for _ in range(10):
                    audio.send_bytes(b"\x00" * 3200)

                seen = []
                speak = None
                for _ in range(20):
                    message = parse_voice_message(control.receive_text())
                    seen.append(message.type)
                    if isinstance(message, VoiceSpeak):
                        speak = message
                        break
                assert speak is not None
                assert "voice.partial" in seen and "voice.speak" in seen
                assert audio.receive_bytes()

                control.send_text(encode_voice_message(VoiceStopRequest(
                    type="voice.stop.request", message_id=uuid4(), sent_at=datetime.now(UTC),
                    voice_session_id=started.voice_session_id,
                    session_epoch=started.session_epoch, reason="user", local_stopped_at=datetime.now(UTC),
                )))
                stopped = recv_until(control, VoiceStopped)
                assert stopped.execution_stopped is True


def test_expired_epoch_audio_is_rejected(world):
    app = create_app(config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/voice", headers=auth(world)) as control:
            control.send_text(encode_voice_message(hello(world)))
            recv_until(control, VoiceWelcome)
            control.send_text(encode_voice_message(VoiceActivate(
                type="voice.activate", message_id=uuid4(), sent_at=datetime.now(UTC),
                request_id=uuid4(), trigger="button", confidence=None,
            )))
            started = recv_until(control, VoiceSessionStarted)
            recv_until(control, VoiceListenStart)
            try:
                with client.websocket_connect(
                    f"ws://127.0.0.1:8000/ws/v1/voice/audio?session_id={started.voice_session_id}&epoch=999",
                    headers=auth(world),
                ):
                    pass
            except WebSocketDisconnect as exc:
                assert exc.code == 1008
            else:
                raise AssertionError("expired epoch accepted")


def test_oversized_audio_frame_is_rejected(world):
    app = create_app(config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/voice", headers=auth(world)) as control:
            control.send_text(encode_voice_message(hello(world)))
            recv_until(control, VoiceWelcome)
            control.send_text(encode_voice_message(VoiceActivate(
                type="voice.activate", message_id=uuid4(), sent_at=datetime.now(UTC),
                request_id=uuid4(), trigger="button", confidence=None,
            )))
            started = recv_until(control, VoiceSessionStarted)
            recv_until(control, VoiceListenStart)
            try:
                with client.websocket_connect(
                    f"ws://127.0.0.1:8000/ws/v1/voice/audio?session_id={started.voice_session_id}&epoch={started.session_epoch}",
                    headers=auth(world),
                ) as audio:
                    audio.send_bytes(b"\x00" * (16 * 1024 + 1))
                    audio.receive_bytes()
            except WebSocketDisconnect as exc:
                assert exc.code == 1008
            else:
                raise AssertionError("oversized frame accepted")


def test_prepared_wake_session_does_not_start_asr_until_activation(world):
    cfg = config(world)
    cfg.nodes[world.node_id].voice.features = frozenset({"capture", "playback", "wake"})
    app = create_app(cfg)
    with TestClient(app, base_url=ORIGIN) as client:
        login = client.post("/api/v1/session", json={"token": world.owner_token.get_secret_value()}, headers={"origin": ORIGIN})
        headers = {"origin": ORIGIN, "x-csrf-token": login.json()["csrf_token"]}
        conversation = client.post("/api/v1/chat/conversations", json={"request_id": str(uuid4())}, headers=headers).json()["conversation"]
        request = uuid4()
        prepared = client.post("/api/v1/voice/sessions", json={"request_id": str(request), "node_id": str(world.node_id),
            "conversation_id": conversation["conversation_id"], "allow_audio_model": True, "allow_model": True,
            "trigger": "wake", "proactive_speak": True}, headers=headers)
        assert prepared.status_code == 200
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/voice", headers=auth(world)) as control:
            value = hello(world).model_copy(update={"activation_request_id": request, "audio_framing": "nxv2"})
            control.send_text(encode_voice_message(value))
            recv_until(control, VoiceWelcome)
            started = recv_until(control, VoiceSessionStarted)
            assert not app.state.voice.asr.sessions
            with client.websocket_connect(
                f"ws://127.0.0.1:8000/ws/v1/voice/audio?session_id={started.voice_session_id}&epoch=1", headers=auth(world)) as audio:
                audio.send_bytes(b"\x00" * 3200)
                assert not app.state.voice.asr.sessions
                control.send_text(encode_voice_message(VoiceActivate(
                    type="voice.activate", message_id=uuid4(), sent_at=datetime.now(UTC),
                    request_id=request, trigger="wake", confidence=0.95)))
                recv_until(control, VoiceListenStart)
                assert app.state.voice.asr.sessions
                control.send_text(encode_voice_message(VoiceStopRequest(
                    type="voice.stop.request", message_id=uuid4(), sent_at=datetime.now(UTC),
                    voice_session_id=started.voice_session_id, session_epoch=1, reason="user", local_stopped_at=datetime.now(UTC))))
                assert recv_until(control, VoiceStopped).execution_stopped
