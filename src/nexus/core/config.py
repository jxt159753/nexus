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
    heartbeat_interval_seconds: StrictInt = Field(default=10, ge=1, le=10)
    heartbeat_timeout_seconds: StrictInt = Field(default=30, ge=2, le=30)
    max_message_bytes: StrictInt = Field(default=MAX_MESSAGE_BYTES, ge=1024, le=MAX_MESSAGE_BYTES)
    max_result_bytes: StrictInt = Field(default=MAX_RESULT_BYTES, ge=128, le=MAX_RESULT_BYTES)
    execution_timeout_seconds: StrictInt = Field(default=10, ge=1, le=10)
    models: ModelConfig = Field(default_factory=ModelConfig)

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
        return self
