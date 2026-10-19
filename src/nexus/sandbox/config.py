"""Strict Sandbox Node configuration. Loading performs no execution."""

import os
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, model_validator
from urllib.parse import urlsplit

from nexus.contracts.base import MAX_RESULT_BYTES, StrictContract


def _absolute_local(path: Path) -> bool:
    text = str(path)
    return path.is_absolute() and not text.startswith(("\\\\", "//")) and "\x00" not in text


class SandboxConfig(StrictContract):
    node_id: UUID
    core_url: str
    local_http: StrictBool = False
    data_dir: Path
    staging_dir: Path
    runsc_path: Path
    runsc_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    platform: Literal["systrap", "kvm"] = "systrap"
    runsc_root: Path
    image_rootfs: Path
    image_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    python_path: str = "/usr/bin/python3"
    max_result_bytes: StrictInt = Field(default=MAX_RESULT_BYTES, ge=128, le=MAX_RESULT_BYTES)
    execution_timeout_seconds: StrictInt = Field(default=10, ge=1, le=10)
    heartbeat_interval_seconds: StrictInt = Field(default=5, ge=1, le=10)
    max_image_entries: StrictInt = Field(default=200000, ge=1, le=1000000)

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        local = (self.data_dir, self.staging_dir, self.runsc_path, self.runsc_root, self.image_rootfs)
        if os.name == "nt":
            raise ValueError("Sandbox Node requires a dedicated Linux execution host")
        if not all(_absolute_local(path) for path in local):
            raise ValueError("absolute local Sandbox paths required")
        url = urlsplit(self.core_url)
        if (url.scheme not in {"ws", "wss"} or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment or url.path != "/ws/v2/sandbox"):
            raise ValueError("fixed Sandbox websocket endpoint required")
        if url.scheme == "ws" and (not self.local_http or url.hostname not in {"127.0.0.1", "::1"}):
            raise ValueError("plaintext Sandbox transport requires explicit literal-loopback development mode")
        if not self.python_path.startswith("/") or ".." in self.python_path:
            raise ValueError("Sandbox Python path must be absolute and fixed")
        return self
