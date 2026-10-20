
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictInt

from nexus.contracts.base import StrictContract
from nexus.contracts.basic import FileReadArguments, FileSearchArguments, FilePreviewArguments, FileWriteArguments, CommandRunArguments


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


class TaskMetadata(StrictContract):
    request_id: UUID
    project_id: UUID | None = None


class SystemInfoRequest(TaskMetadata):
    intent: Literal["get_system_info"]
    arguments: SystemInfoArguments


class ListDirectoryRequest(TaskMetadata):
    intent: Literal["list_directory"]
    arguments: ListDirectoryArguments


class FileReadRequest(TaskMetadata):
    intent: Literal["read_file"]
    arguments: FileReadArguments


class FileSearchRequest(TaskMetadata):
    intent: Literal["find_files"]
    arguments: FileSearchArguments


class FilePreviewRequest(TaskMetadata):
    intent: Literal["preview_file_write"]
    arguments: FilePreviewArguments


class FileWriteRequest(TaskMetadata):
    intent: Literal["write_file"]
    arguments: FileWriteArguments


class CommandRunRequest(TaskMetadata):
    intent: Literal["run_command"]
    arguments: CommandRunArguments


TaskRequest = Annotated[
    SystemInfoRequest | ListDirectoryRequest | FileReadRequest | FileSearchRequest | FilePreviewRequest | FileWriteRequest | CommandRunRequest,
    Field(discriminator="intent"),
]
