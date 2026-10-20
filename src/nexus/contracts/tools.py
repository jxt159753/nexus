
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator
from nexus.contracts.basic import FileReadArguments, FileSearchArguments, FilePreviewArguments, FileWriteArguments, CommandRunArguments, relative_path, text_digest, write_digest
from nexus.contracts.base import CommandDigest

from nexus.contracts.base import MAX_RESULT_BYTES, StrictContract, Version1

from nexus.contracts.task import (
    ListDirectoryArguments,
    SystemInfoArguments,
)


class ToolResultContract(StrictContract):
    @model_validator(mode="after")
    def check_result_bytes(self) -> Self:
        if len(self.model_dump_json().encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("tool result exceeds 32 KiB")
        return self


class SystemInfoResult(ToolResultContract):
    os_name: StrictStr = Field(min_length=1, max_length=100)
    os_version: StrictStr = Field(min_length=1, max_length=200)
    architecture: StrictStr = Field(min_length=1, max_length=100)
    python_version: StrictStr = Field(min_length=1, max_length=100)


class DirectoryEntry(StrictContract):
    name: StrictStr = Field(min_length=1, max_length=255)
    kind: Literal["file", "directory", "link", "other"]


class ListDirectoryResult(ToolResultContract):
    root_id: Literal["workspace"]
    entries: list[DirectoryEntry] = Field(max_length=200)
    truncated: StrictBool


class SystemInfoCall(StrictContract):
    tool: Literal["system.info"]
    tool_version: Version1
    arguments: SystemInfoArguments


class ListDirectoryCall(StrictContract):
    tool: Literal["filesystem.list"]
    tool_version: Version1
    arguments: ListDirectoryArguments


class FileReadResult(ToolResultContract):
    root_id: Literal["workspace"]
    path: StrictStr
    text: StrictStr
    sha256: CommandDigest
    size_bytes: StrictInt = Field(ge=0)
    truncated: StrictBool


class FileSearchResult(ToolResultContract):
    root_id: Literal["workspace"]
    paths: list[StrictStr] = Field(max_length=100)
    scanned: StrictInt = Field(ge=0, le=2000)
    truncated: StrictBool


class FilePreviewResult(ToolResultContract):
    root_id: Literal["workspace"]
    path: StrictStr
    previous_sha256: CommandDigest | None
    content_sha256: CommandDigest
    preview_digest: CommandDigest
    diff: StrictStr = Field(max_length=16000)
    truncated: StrictBool


class FileWriteResult(ToolResultContract):
    root_id: Literal["workspace"]
    path: StrictStr
    sha256: CommandDigest
    bytes_written: StrictInt = Field(ge=0, le=12000)


class CommandRunResult(ToolResultContract):
    root_id: Literal["workspace"]
    command_id: StrictStr
    template_digest: CommandDigest
    exit_code: StrictInt
    stdout: StrictStr
    stderr: StrictStr
    truncated: StrictBool


class FileReadCall(StrictContract):
    tool: Literal["filesystem.read"]
    tool_version: Version1
    arguments: FileReadArguments


class FileSearchCall(StrictContract):
    tool: Literal["filesystem.search"]
    tool_version: Version1
    arguments: FileSearchArguments


class FilePreviewCall(StrictContract):
    tool: Literal["filesystem.write_preview"]
    tool_version: Version1
    arguments: FilePreviewArguments


class FileWriteCall(StrictContract):
    tool: Literal["filesystem.write"]
    tool_version: Version1
    arguments: FileWriteArguments


class CommandRunCall(StrictContract):
    tool: Literal["command.run"]
    tool_version: Version1
    arguments: CommandRunArguments


ToolCall = Annotated[SystemInfoCall | ListDirectoryCall | FileReadCall | FileSearchCall | FilePreviewCall | FileWriteCall | CommandRunCall, Field(discriminator="tool")]
ToolResult = SystemInfoResult | ListDirectoryResult | FileReadResult | FileSearchResult | FilePreviewResult | FileWriteResult | CommandRunResult


TOOL_ARGUMENT_MODELS = {
    "system.info@1": SystemInfoArguments,
    "filesystem.list@1": ListDirectoryArguments,
    "filesystem.read@1": FileReadArguments,
    "filesystem.search@1": FileSearchArguments,
    "filesystem.write_preview@1": FilePreviewArguments,
    "filesystem.write@1": FileWriteArguments,
    "command.run@1": CommandRunArguments,
}

TOOL_RESULT_MODELS = {
    "system.info@1": SystemInfoResult,
    "filesystem.list@1": ListDirectoryResult,
    "filesystem.read@1": FileReadResult,
    "filesystem.search@1": FileSearchResult,
    "filesystem.write_preview@1": FilePreviewResult,
    "filesystem.write@1": FileWriteResult,
    "command.run@1": CommandRunResult,
}


def validate_result(command: ToolCall, result: ToolResult, max_bytes: int) -> ToolResult:
    """Shared schema/constraint validation; no platform or authority imports."""
    model = TOOL_RESULT_MODELS[f"{command.tool}@{command.tool_version}"]
    checked = model.model_validate_json(result.model_dump_json())
    args = command.arguments
    if hasattr(checked, "root_id") and checked.root_id != args.root_id:
        raise ValueError("resource mismatch")
    if hasattr(checked, "path") and checked.path != args.path:
        raise ValueError("path mismatch")
    if isinstance(checked, FileReadResult):
        size = len(checked.text.encode())
        if size > args.max_bytes or size > checked.size_bytes or (not checked.truncated and (size != checked.size_bytes or text_digest(checked.text) != checked.sha256)):
            raise ValueError("invalid read evidence")
    if isinstance(checked, FileSearchResult):
        if len(set(checked.paths)) != len(checked.paths) or len(checked.paths) > args.limit:
            raise ValueError("invalid search evidence")
        import fnmatch
        for path in checked.paths:
            relative_path(path)
            if len(path.split("/"))-1 > args.max_depth or not fnmatch.fnmatchcase(path.rsplit("/",1)[-1].casefold(), args.pattern.casefold()):
                raise ValueError("search result outside query")
    if isinstance(checked, FilePreviewResult):
        if checked.content_sha256 != text_digest(args.content) or checked.preview_digest != write_digest(args.root_id, args.path, checked.previous_sha256, checked.content_sha256):
            raise ValueError("preview mismatch")
    if isinstance(checked, FileWriteResult):
        if checked.sha256 != text_digest(args.content) or checked.bytes_written != len(args.content.encode()):
            raise ValueError("write evidence mismatch")
    if isinstance(checked, CommandRunResult):
        if checked.command_id != args.command_id or checked.template_digest != args.template_digest:
            raise ValueError("command evidence mismatch")
    if len(checked.model_dump_json().encode("utf-8")) > max_bytes:
        raise ValueError("result exceeds byte budget")
    if isinstance(checked, ListDirectoryResult):
        if (not isinstance(command, ListDirectoryCall)
                or checked.root_id != command.arguments.root_id
                or len(checked.entries) > command.arguments.limit):
            raise ValueError("directory evidence does not match command")
        names = [entry.name for entry in checked.entries]
        if (len(set(names)) != len(names) or any(
            name in {".", ".."} or any(char in name for char in ("/", "\\", "\x00", ":"))
            for name in names
        )):
            raise ValueError("directory entries must be unique direct names")
    return checked
