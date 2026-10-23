"""Core process and trusted capability configuration."""

from pathlib import Path
from typing import Literal, Self
from uuid import UUID
from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator
from nexus.contracts.base import MAX_MESSAGE_BYTES, MAX_RESULT_BYTES, StrictContract
from nexus.core.models.config import ModelConfig
from nexus.plugins.config import PluginConfig
from nexus.contracts.basic import ToolKey, CommandId
from nexus.contracts.base import CommandDigest



class TrustedNode(StrictContract):
    tools: frozenset[ToolKey] = frozenset()
    roots: frozenset[Literal["workspace"]] = frozenset()
    resource_version: StrictInt = Field(default=1, ge=1)
    command_digests: dict[CommandId, CommandDigest] = Field(default_factory=dict, max_length=16)
    interaction_digests: dict[CommandId, CommandDigest] = Field(default_factory=dict, max_length=16)
    plugin_digest: CommandDigest | None=None
    voice: "VoiceNodePolicy" = Field(default_factory=lambda: VoiceNodePolicy())


class VoiceNodePolicy(StrictContract):
    """Owner trust for one node's voice runtime; advertised features never grant this."""

    enabled: StrictBool = False
    features: frozenset[
        Literal["capture", "playback", "wake", "streaming_asr", "streaming_tts", "aec", "device_switch"]
    ] = frozenset()
    max_session_seconds: StrictInt = Field(default=1800, ge=30, le=3600)


class VoiceConfig(StrictContract):
    """Core media-proxy limits. Provider credentials are environment-only."""

    enabled: StrictBool = False
    provider: Literal["fake", "bailian"] = "fake"
    workspace_id: StrictStr = ""
    region: Literal["cn-beijing", "ap-southeast-1"] = "cn-beijing"
    voice_id: StrictStr = Field(default="", max_length=100)
    asr_model: Literal["fun-asr-realtime", "fun-asr-realtime-2026-02-28"] = "fun-asr-realtime-2026-02-28"
    tts_model: Literal["cosyvoice-v3.5-flash"] = "cosyvoice-v3.5-flash"
    heartbeat_interval_seconds: StrictInt = Field(default=5, ge=1, le=10)
    heartbeat_timeout_seconds: StrictInt = Field(default=15, ge=2, le=30)
    max_session_seconds: StrictInt = Field(default=1800, ge=30, le=3600)
    max_final_chars: StrictInt = Field(default=2000, ge=1, le=2000)
    max_concurrent_sessions: StrictInt = Field(default=1, ge=1, le=4)
    max_turns: StrictInt = Field(default=30, ge=1, le=30)
    idle_timeout_seconds: StrictInt = Field(default=30, ge=5, le=60)
    max_utterance_seconds: StrictInt = Field(default=60, ge=5, le=60)
    max_tts_chars: StrictInt = Field(default=1000, ge=100, le=2000)
    max_tts_seconds: StrictInt = Field(default=180, ge=10, le=300)
    daily_asr_seconds: StrictInt = Field(default=1800, ge=60, le=86400)
    daily_tts_chars: StrictInt = Field(default=20000, ge=1000, le=1000000)
    tts_rate: float = Field(default=1.05, ge=0.5, le=2.0)
    tts_pitch: float = Field(default=1.0, ge=0.5, le=2.0)
    tts_volume: StrictInt = Field(default=50, ge=0, le=100)
    asr_cny_microunits_per_second: StrictInt = Field(default=330, ge=1, le=1000000)
    tts_cny_microunits_per_char: StrictInt = Field(default=80, ge=1, le=1000000)
    dnd_start_hour: StrictInt = Field(default=22, ge=0, le=23)
    dnd_end_hour: StrictInt = Field(default=8, ge=0, le=23)
    proactive_daily_limit: StrictInt = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def check_provider(self) -> Self:
        if self.heartbeat_timeout_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("voice heartbeat timeout must exceed interval")
        if self.enabled and self.provider == "bailian":
            if not self.workspace_id.strip() or not self.voice_id.strip():
                raise ValueError("Bailian voice requires workspace_id and voice_id")
        return self



TrustedNode.model_rebuild()

class SandboxNodePolicy(StrictContract):
    """Owner-registered sandbox host; self-reported capability never grants this."""

    runtime: Literal["runsc"] = "runsc"
    platform: Literal["systrap", "kvm"] = "systrap"
    runsc_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    image_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_profiles: frozenset[Literal["directory_entry_stats_v1"]] = frozenset(
        {"directory_entry_stats_v1"})


class ExtensionConfig(StrictContract):
    enabled: StrictBool = False
    generation_enabled: StrictBool = False
    fixture_generation: StrictBool = False
    max_output_tokens: StrictInt = Field(default=4096, ge=256, le=4096)
    approval_ttl_hours: StrictInt = Field(default=168, ge=1, le=168)
    sandbox_test_timeout_seconds: StrictInt = Field(default=60, ge=10, le=60)
    max_candidates: StrictInt = Field(default=64, ge=1, le=256)
    max_total_bytes: StrictInt = Field(default=4 * 1024 * 1024, ge=65536, le=16 * 1024 * 1024)
    max_skill_runs: StrictInt = Field(default=10000, ge=1, le=10000)
    max_sandbox_executions: StrictInt = Field(default=10000, ge=1, le=10000)
    max_test_runs: StrictInt = Field(default=10000, ge=1, le=10000)


class CoreConfig(StrictContract):
    data_dir: Path
    owner_id: UUID
    host: str = "127.0.0.1"
    port: StrictInt = Field(default=8000, ge=1, le=65535)
    local_http: StrictBool = False
    origins: frozenset[str] = Field(min_length=1, max_length=10)
    tls_cert: Path | None = None
    tls_key: Path | None = None
    nodes: dict[UUID, TrustedNode] = Field(default_factory=dict, max_length=32)
    sandbox_nodes: dict[UUID, SandboxNodePolicy] = Field(default_factory=dict, max_length=8)
    heartbeat_interval_seconds: StrictInt = Field(default=10, ge=1, le=10)
    heartbeat_timeout_seconds: StrictInt = Field(default=30, ge=2, le=30)
    max_message_bytes: StrictInt = Field(default=MAX_MESSAGE_BYTES, ge=1024, le=MAX_MESSAGE_BYTES)
    max_result_bytes: StrictInt = Field(default=MAX_RESULT_BYTES, ge=128, le=MAX_RESULT_BYTES)
    execution_timeout_seconds: StrictInt = Field(default=10, ge=1, le=10)
    models: ModelConfig = Field(default_factory=ModelConfig)
    extensions: ExtensionConfig = Field(default_factory=ExtensionConfig)
    plugins: PluginConfig = Field(default_factory=PluginConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)

    @model_validator(mode="after")
    def check_service(self) -> Self:
        from nexus.core.security.http import TransportPolicy
        if not self.data_dir.is_absolute() or str(self.data_dir).startswith(("\\\\", "//")):
            raise ValueError("absolute local data directory required")
        TransportPolicy(self.origins, local_http=self.local_http)
        if self.local_http:
            if self.host not in {"127.0.0.1", "::1"} or self.tls_cert is not None or self.tls_key is not None:
                raise ValueError("development transport must bind a literal loopback")
        elif self.tls_cert is None or self.tls_key is None:
            raise ValueError("remote service requires explicit TLS certificate and key")
        if self.heartbeat_timeout_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("heartbeat timeout must exceed interval")
        # Leave enough room for the nested status/result envelope and metadata.
        if self.max_result_bytes + 2048 > self.max_message_bytes:
            raise ValueError("result budget must leave 2048 bytes for metadata")
        if (self.extensions.fixture_generation or self.extensions.generation_enabled) and not self.extensions.enabled:
            raise ValueError("extension generation requires the extension feature to be enabled")
        worst = (16384*self.models.input_price + self.extensions.max_output_tokens*self.models.output_price + 999999)//1000000
        if self.models.cost_limits_enabled and self.models.enabled and self.extensions.generation_enabled and self.models.task_cost_microunits and worst > self.models.task_cost_microunits:
            raise ValueError("extension token price ceiling exceeds request budget")
        return self
