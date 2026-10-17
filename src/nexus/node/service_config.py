"""Connection settings separate from local execution policy and credentials."""

from pathlib import Path
from typing import Self
from urllib.parse import urlsplit
from pydantic import StrictBool, model_validator
from nexus.node.config import NodeConfig

class NodeServiceConfig(NodeConfig):
    core_url: str
    local_http: StrictBool = False
    ca_file: Path | None = None

    @model_validator(mode="after")
    def check_connection(self) -> Self:
        parsed = urlsplit(self.core_url)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path != "/ws/v1/node" or len(self.core_url) > 2048):
            raise ValueError("explicit node WebSocket endpoint required")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("valid endpoint port required")
        if parsed.scheme == "wss":
            return self
        if not (self.local_http and parsed.scheme == "ws" and parsed.hostname in {"127.0.0.1", "::1"}):
            raise ValueError("WSS required except explicit literal loopback development")
        return self
