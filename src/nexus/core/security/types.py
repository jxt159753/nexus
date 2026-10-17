from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import SecretStr


class AccessDenied(PermissionError):
    def __init__(self, code: str = "FORBIDDEN"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Principal:
    identity_id: UUID
    role: Literal["owner", "node"]
    credential_hash: str = field(repr=False)
    session_id: UUID | None = None


@dataclass(frozen=True)
class BrowserSession:
    session_id: UUID
    token: SecretStr
    csrf: SecretStr
    expires_at: datetime


@dataclass(frozen=True)
class NodeScope:
    """Only reduces persisted grants. Link supplies the current authenticated scope."""
    node_id: UUID
    tools: frozenset[str]
    roots: frozenset[str]
    local_tools: frozenset[str]
    local_roots: frozenset[str]
    resource_version: int = 1

    def __post_init__(self) -> None:
        if type(self.resource_version) is not int or self.resource_version < 1:
            raise ValueError("positive resource policy version required")


@dataclass(frozen=True)
class Authorization:
    mode: Literal["allow", "approval_required"]
    policy_version: int
    approval_id: UUID | None = None
    expires_at: datetime | None = None
