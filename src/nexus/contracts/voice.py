"""Strict voice control contracts. No audio DSP, provider I/O or authorization."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, TypeAdapter, ValidationError

from nexus.contracts.base import CommandDigest, StrictContract, UTCDateTime, Version1
from nexus.contracts.errors import ErrorCode, ProtocolError

MAX_VOICE_MESSAGE_BYTES = 16 * 1024


class VoiceConsent(StrictContract):
    """Owner-authored activation; a Node cannot create or expand this consent."""

    request_id: UUID
    node_id: UUID
    conversation_id: UUID
    allow_audio_model: StrictBool
    allow_model: StrictBool
    include_context: StrictBool = True
    allow_tools: StrictBool = False
    allow_work_results: StrictBool = False
    allow_compaction: StrictBool = False
    proactive_speak: StrictBool = False
    proactive_listen: StrictBool = False
    trigger: Literal["button", "wake"] = "button"


def pack_voice_audio(generation: int, data: bytes = b"", *, end: bool = False) -> bytes:
    import struct
    if not 0 <= generation <= 0xFFFFFFFF or len(data) > 8192 or len(data) % 2:
        raise ValueError("invalid voice PCM packet")
    return struct.pack("!4sIB", b"NXV2", generation, int(end)) + data


def unpack_voice_audio(packet: bytes) -> tuple[int, bytes, bool]:
    import struct
    if not 9 <= len(packet) <= 8201:
        raise ValueError("invalid voice audio packet size")
    magic, generation, flag = struct.unpack("!4sIB", packet[:9])
    data = packet[9:]
    if magic != b"NXV2" or flag not in {0, 1} or len(data) % 2 or flag and data:
        raise ValueError("invalid voice audio packet")
    return generation, data, bool(flag)


class VoiceBase(StrictContract):
    protocol_version: Version1 = 1
    message_id: UUID
    correlation_id: UUID | None = None
    sent_at: UTCDateTime


class VoiceHello(VoiceBase):
    type: Literal["voice.hello"]
    node_id: UUID
    voice_version: StrictStr = Field(min_length=1, max_length=50)
    boot_id: UUID
    features: list[
        Literal["capture", "playback", "wake", "streaming_asr", "streaming_tts", "aec", "device_switch"]
    ] = Field(default_factory=list, max_length=8)
    models: dict[StrictStr, CommandDigest] = Field(default_factory=dict, max_length=8)
    config_digest: CommandDigest | None = None
    audio_framing: Literal["pcm16", "nxv2"] = "pcm16"
    activation_request_id: UUID | None = None


class VoiceWelcome(VoiceBase):
    type: Literal["voice.welcome"]
    connection_id: UUID
    heartbeat_interval_seconds: StrictInt = Field(ge=1, le=10)
    heartbeat_timeout_seconds: StrictInt = Field(ge=2, le=30)
    policy_revision: StrictInt = Field(ge=1)
    max_session_seconds: StrictInt = Field(ge=30, le=3600)
    max_final_chars: StrictInt = Field(ge=1, le=2000)


class VoiceActivate(VoiceBase):
    type: Literal["voice.activate"]
    request_id: UUID
    trigger: Literal["button", "wake"]
    confidence: Annotated[float | None, Field(default=None, ge=0.0, le=1.0)]
    device_id: UUID | None = None
    conversation_id: UUID | None = None


class VoiceStopped(VoiceBase):
    type: Literal["voice.session.stopped"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    reason: StrictStr = Field(min_length=1, max_length=100)
    capture_stopped_at: UTCDateTime | None = None
    playback_stopped_at: UTCDateTime | None = None
    dropped_frames: StrictInt = Field(default=0, ge=0)
    tts_cancelled: StrictBool = False
    asr_cancelled: StrictBool = False
    execution_stopped: StrictBool = True


class VoiceSessionStarted(VoiceBase):
    type: Literal["voice.session.started"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    conversation_id: UUID
    mode: Literal["push_to_talk", "wake", "proactive"]
    expires_at: UTCDateTime


class VoiceListenStart(VoiceBase):
    type: Literal["voice.listen.start"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    utterance_id: UUID
    max_seconds: StrictInt = Field(ge=1, le=60)
    language: Literal["zh", "en", "zh-en"] = "zh"


class VoiceListenStop(VoiceBase):
    type: Literal["voice.listen.stop"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    reason: StrictStr = Field(min_length=1, max_length=100)


class VoicePartial(VoiceBase):
    type: Literal["voice.partial"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    utterance_id: UUID
    seq: StrictInt = Field(ge=0)
    text: StrictStr = Field(min_length=1, max_length=200)
    stable: StrictBool = False


class VoiceFinal(VoiceBase):
    type: Literal["voice.final"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    utterance_id: UUID
    text: StrictStr = Field(min_length=1, max_length=2000)
    started_at: UTCDateTime
    ended_at: UTCDateTime
    endpoint_reason: StrictStr = Field(default="vad", min_length=1, max_length=50)


class VoiceSpeak(VoiceBase):
    type: Literal["voice.speak"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    utterance_id: UUID
    generation: StrictInt = Field(ge=0)
    segment_index: StrictInt = Field(ge=0)
    text: StrictStr = Field(min_length=1, max_length=500)
    final_segment: StrictBool = False
    not_after: UTCDateTime
    voice: StrictStr | None = Field(default=None, max_length=100)
    speed: Annotated[float, Field(ge=0.5, le=2.0)] = 1.0


class VoiceSpeakStop(VoiceBase):
    type: Literal["voice.speak.stop"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    utterance_id: UUID
    generation: StrictInt = Field(ge=0)
    reason: Literal["barge_in", "user_stop", "disconnect", "error"]
    at_segment: StrictInt = Field(default=0, ge=0)


class VoiceSpeakDone(VoiceBase):
    type: Literal["voice.speak.done"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    utterance_id: UUID
    generation: StrictInt = Field(ge=0)
    segment_index: StrictInt = Field(ge=0)
    played_ms: StrictInt = Field(ge=0)


class VoiceSpeakCompleted(VoiceBase):
    type: Literal["voice.speak.completed"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    utterance_id: UUID
    generation: StrictInt = Field(ge=0)
    segments: StrictInt = Field(ge=0)
    total_ms: StrictInt = Field(ge=0)


class VoiceBargeIn(VoiceBase):
    type: Literal["voice.barge_in"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    utterance_id: UUID
    generation: StrictInt = Field(ge=0)
    detected_at: UTCDateTime
    playback_position_ms: StrictInt = Field(ge=0)
    segment_index: StrictInt = Field(ge=0)
    confidence: Annotated[float | None, Field(default=None, ge=0.0, le=1.0)]
    source: Literal["vad", "kws", "button"] = "vad"


class VoiceStopRequest(VoiceBase):
    type: Literal["voice.stop.request"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    reason: StrictStr = Field(min_length=1, max_length=100)
    local_stopped_at: UTCDateTime


class VoiceStop(VoiceBase):
    type: Literal["voice.stop"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    reason: Literal["user", "core", "timeout", "shutdown", "policy", "disconnect"]


class VoiceState(VoiceBase):
    type: Literal["voice.state"]
    voice_session_id: UUID
    session_epoch: StrictInt = Field(ge=1)
    state: Literal["activating", "listening", "thinking", "speaking", "interrupted", "stopping", "stopped", "error"]
    since: UTCDateTime
    detail: StrictStr = Field(default="", max_length=200)
    generation: StrictInt = Field(default=0, ge=0)
    utterance_id: UUID | None = None


class VoiceHeartbeat(VoiceBase):
    type: Literal["voice.heartbeat"]
    voice_session_id: UUID | None = None
    session_epoch: StrictInt | None = Field(default=None, ge=1)
    state: Literal["standby", "activating", "listening", "thinking", "speaking", "interrupted", "stopping", "stopped", "error"] = "standby"
    active_utterance_id: UUID | None = None
    capture_active: StrictBool = False
    playback_active: StrictBool = False


class VoiceError(VoiceBase):
    type: Literal["voice.error"]
    code: ErrorCode
    message: StrictStr = Field(min_length=1, max_length=200)
    retryable: StrictBool = False
    voice_session_id: UUID | None = None
    session_epoch: StrictInt | None = Field(default=None, ge=1)


VoiceMessage = Annotated[
    VoiceHello | VoiceWelcome | VoiceActivate | VoiceSessionStarted | VoiceStopped
    | VoiceListenStart | VoiceListenStop | VoicePartial | VoiceFinal | VoiceSpeak
    | VoiceSpeakStop | VoiceSpeakDone | VoiceSpeakCompleted | VoiceBargeIn
    | VoiceStopRequest | VoiceStop | VoiceState | VoiceHeartbeat | VoiceError,
    Field(discriminator="type"),
]
VOICE_MESSAGE = TypeAdapter(VoiceMessage)


def parse_voice_message(data: str | bytes) -> VoiceMessage:
    """Validate one complete UTF-8 control frame; binary audio has its own endpoint."""
    if len(data) > MAX_VOICE_MESSAGE_BYTES:
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Voice message exceeds 16 KiB")
    try:
        raw = data.encode("utf-8") if isinstance(data, str) else data
    except UnicodeError:
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Invalid UTF-8 voice message") from None
    if len(raw) > MAX_VOICE_MESSAGE_BYTES:
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Voice message exceeds 16 KiB")
    try:
        return VOICE_MESSAGE.validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False)
        if any(len(item["loc"]) == 2 and item["loc"][1] == "protocol_version" for item in errors):
            raise ProtocolError(ErrorCode.PROTOCOL_UNSUPPORTED, "Unsupported voice protocol version") from None
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "Invalid voice message") from None


def encode_voice_message(message: VoiceMessage) -> str:
    import json
    raw = json.dumps(message.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
    return parse_voice_message(raw).model_dump_json()
