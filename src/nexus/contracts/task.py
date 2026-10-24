from nexus.plugins.contracts import PluginNodeArguments

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictInt

from nexus.contracts.interaction import ObserveArguments, InteractArguments
from nexus.contracts.images import ImageReadArguments
from nexus.contracts.base import StrictContract
from nexus.contracts.basic import FileLinesArguments, FileGlobArguments, FileGrepArguments, FileEditPreviewArguments, FileEditArguments, FileReadArguments, FileSearchArguments, FilePreviewArguments, FileWriteArguments, CommandRunArguments


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


class ImageReadRequest(TaskMetadata):
    intent: Literal["read_image"]
    arguments: ImageReadArguments


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


class FileLinesRequest(TaskMetadata):
    intent: Literal["read_file_lines"]
    arguments: FileLinesArguments


class FileGlobRequest(TaskMetadata):
    intent: Literal["glob_files"]
    arguments: FileGlobArguments


class FileGrepRequest(TaskMetadata):
    intent: Literal["search_file_content"]
    arguments: FileGrepArguments


class FileEditPreviewRequest(TaskMetadata):
    intent: Literal["preview_file_edit"]
    arguments: FileEditPreviewArguments


class FileEditRequest(TaskMetadata):
    intent: Literal["edit_file"]
    arguments: FileEditArguments


class ObserveRequest(TaskMetadata):
    intent: Literal["observe_target"]
    arguments: ObserveArguments


class InteractRequest(TaskMetadata):
    intent: Literal["interact_target"]
    arguments: InteractArguments


class PluginNodeRequest(TaskMetadata):
    intent: Literal["execute_plugin"]
    arguments: PluginNodeArguments


TaskRequest = Annotated[
    SystemInfoRequest | ListDirectoryRequest | FileReadRequest | FileSearchRequest | FilePreviewRequest | FileWriteRequest | CommandRunRequest | FileLinesRequest | FileGlobRequest | FileGrepRequest | FileEditPreviewRequest | FileEditRequest | ObserveRequest | InteractRequest | ImageReadRequest | PluginNodeRequest,
    Field(discriminator="intent"),
]
