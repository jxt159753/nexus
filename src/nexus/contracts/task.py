
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictInt

from nexus.contracts.base import StrictContract


TaskStatus = Literal[
    "queued", "awaiting_approval", "ready", "dispatching", "running",
    "recovering", "verifying", "succeeded", "failed", "cancelled", "timed_out",
]
ExecutionOutcome = Literal["not_started", "known", "unknown"]


class SystemInfoArguments(StrictContract):
    pass


class ListDirectoryArguments(StrictContract):
    root_id: Literal["workspace"]
    limit: Annotated[StrictInt, Field(ge=1, le=200)]


class SystemInfoRequest(StrictContract):
    request_id: UUID
    intent: Literal["get_system_info"]
    arguments: SystemInfoArguments


class ListDirectoryRequest(StrictContract):
    request_id: UUID
    intent: Literal["list_directory"]
    arguments: ListDirectoryArguments


TaskRequest = Annotated[
    SystemInfoRequest | ListDirectoryRequest,
    Field(discriminator="intent"),
]
