"""Windows voice runtime configuration. Secrets are environment-only."""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import tomllib


@dataclass(frozen=True)
class RuntimeConfig:
    core_url: str
    node_id: UUID
    input_device: str | int | None = None
    output_device: str | int | None = None
    allow_device_fallback: bool = False
    capture_rate: int = 48000
    uplink_rate: int = 16000
    uplink_batch_ms: int = 40
    downlink_rate: int = 22050
    output_rate: int = 48000
    playback_buffer_ms: int = 200
    aec_enabled: bool = True
    wake_enabled: bool = False
    wake_model_dir: str = "models/kws"
    wake_keywords: str = "config/voice-keywords.example.txt"
    wake_encoder: str = "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx"
    wake_decoder: str = "decoder-epoch-13-avg-2-chunk-16-left-64.onnx"
    wake_joiner: str = "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx"
    wake_tokens: str = "tokens.txt"
    wake_keywords_score: float = 1.5
    wake_threshold: float = 0.25
    wake_trailing_blanks: int = 2
    start_on_launch: bool = True
    stay_alive: bool = False
    token_env: str = "NEXUS_NODE_TOKEN"
    origin: str | None = None
    heartbeat_grace_ms: int = 2000
    request_id: UUID | None = None
    conversation_id: UUID | None = None
    ipc: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.core_url)
        if parsed.scheme not in {"wss", "ws"} or not parsed.netloc:
            raise ValueError("core_url must be a ws:// or wss:// URL")
        if parsed.scheme == "ws" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("plain ws:// is allowed only on loopback")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("core_url must not contain a path")
        if not 8000 <= self.capture_rate <= 48000:
            raise ValueError("capture_rate out of range")
        if self.uplink_rate != 16000:
            raise ValueError("Fun-ASR uplink must be 16 kHz")
        if self.capture_rate != 48000 or self.output_rate != 48000 or self.downlink_rate != 22050:
            raise ValueError("voice runtime requires 48 kHz capture/output and 22.05 kHz downlink")
        if self.uplink_batch_ms not in {20, 40, 60, 80, 100}:
            raise ValueError("uplink_batch_ms must be 20..100 ms")
        if not 20 <= self.playback_buffer_ms <= 1000:
            raise ValueError("playback_buffer_ms out of range")
        if not 1 <= self.heartbeat_grace_ms <= 10000:
            raise ValueError("heartbeat_grace_ms out of range")
        if self.wake_enabled and not self.wake_model_dir:
            raise ValueError("wake_model_dir is required when wake is enabled")


def load_runtime_config(path: Path) -> RuntimeConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported voice runtime schema_version")
    core = data.get("core", {})
    audio = data.get("audio", {})
    device = data.get("device", {})
    values = {
        "core_url": core.get("url", ""),
        "node_id": UUID(str(core.get("node_id", ""))),
        "input_device": device.get("input") or None,
        "output_device": device.get("output") or None,
        "allow_device_fallback": bool(device.get("allow_fallback", False)),
        "capture_rate": int(audio.get("capture_rate", 48000)),
        "uplink_rate": int(audio.get("uplink_rate", 16000)),
        "uplink_batch_ms": int(audio.get("uplink_batch_ms", 40)),
        "downlink_rate": int(audio.get("downlink_rate", 22050)),
        "output_rate": int(audio.get("output_rate", 48000)),
        "playback_buffer_ms": int(audio.get("playback_buffer_ms", 200)),
        "aec_enabled": bool(audio.get("aec_enabled", True)),
        "start_on_launch": bool(data.get("start_on_launch", True)),
        "stay_alive": bool(data.get("stay_alive", False)),
        "token_env": str(core.get("token_env", "NEXUS_NODE_TOKEN")),
        "origin": core.get("origin"),
        "request_id": UUID(core["request_id"]) if core.get("request_id") else None,
        "conversation_id": UUID(core["conversation_id"]) if core.get("conversation_id") else None,
    }
    wake = data.get("wake", {})
    values.update({
        "wake_enabled": bool(wake.get("enabled", False)),
        "wake_model_dir": str(wake.get("model_dir", values.get("wake_model_dir", "models/kws"))),
        "wake_keywords": str(wake.get("keywords", "config/voice-keywords.example.txt")),
        "wake_encoder": str(wake.get("encoder", "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx")),
        "wake_decoder": str(wake.get("decoder", "decoder-epoch-13-avg-2-chunk-16-left-64.onnx")),
        "wake_joiner": str(wake.get("joiner", "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx")),
        "wake_tokens": str(wake.get("tokens", "tokens.txt")),
        "wake_keywords_score": float(wake.get("keywords_score", 1.5)),
        "wake_threshold": float(wake.get("threshold", 0.25)),
        "wake_trailing_blanks": int(wake.get("trailing_blanks", 2)),
    })
    return RuntimeConfig(**values)
