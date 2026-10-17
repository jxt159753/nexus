
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictStr, model_validator

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


ToolCall = Annotated[SystemInfoCall | ListDirectoryCall, Field(discriminator="tool")]
ToolResult = SystemInfoResult | ListDirectoryResult


TOOL_ARGUMENT_MODELS = {
    "system.info@1": SystemInfoArguments,
    "filesystem.list@1": ListDirectoryArguments,
}

TOOL_RESULT_MODELS = {
    "system.info@1": SystemInfoResult,
    "filesystem.list@1": ListDirectoryResult,
}
