"""Core process and trusted capability configuration."""

from pathlib import Path
from typing import Literal, Self
from uuid import UUID
from pydantic import Field, StrictBool, StrictInt, model_validator
from nexus.contracts.base import MAX_MESSAGE_BYTES, MAX_RESULT_BYTES, StrictContract
from nexus.core.models.config import ModelConfig

ToolKey = Literal["system.info@1", "filesystem.list@1"]


class TrustedNode(StrictContract):
    tools: frozenset[ToolKey] = frozenset()
    roots: frozenset[Literal["workspace"]] = frozenset()
    resource_version: StrictInt = Field(default=1, ge=1)


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
        if self.models.enabled and self.extensions.generation_enabled and self.models.task_cost_microunits and worst > self.models.task_cost_microunits:
            raise ValueError("extension token price ceiling exceeds request budget")
        return self
