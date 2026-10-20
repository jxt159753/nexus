"""Bounded, explicit workspace operations; no arbitrary paths or shell strings."""

import hashlib
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictInt, StrictStr, model_validator

from nexus.contracts.base import CommandDigest, StrictContract

ToolName = Literal["system.info", "filesystem.list", "filesystem.read", "filesystem.search", "filesystem.write_preview", "filesystem.write", "command.run"]
ToolKey = Literal["system.info@1", "filesystem.list@1", "filesystem.read@1", "filesystem.search@1", "filesystem.write_preview@1", "filesystem.write@1", "command.run@1"]
CommandId = Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]


def relative_path(value: str) -> str:
    # Canonical POSIX spelling on the wire, with portable Windows restrictions.
    if (len(value.encode()) > 240 or value.startswith("/") or "\\" in value or ":" in value
            or any(ord(char) < 32 for char in value) or any(char in value for char in '*?<>|"')):
        raise ValueError("canonical workspace-relative path required")
    for part in value.split("/"):
        if (not part or part in {".", ".."} or part.endswith((".", " "))
                or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}):
            raise ValueError("unsafe path component")
    return value


class FileReadArguments(StrictContract):
    root_id: Literal["workspace"]
    path: StrictStr = Field(min_length=1, max_length=240)
    max_bytes: StrictInt = Field(default=8192, ge=1, le=8192)

    @model_validator(mode="after")
    def path_safe(self):
        relative_path(self.path)
        return self


class FileSearchArguments(StrictContract):
    root_id: Literal["workspace"]
    pattern: StrictStr = Field(min_length=1, max_length=100)
    limit: StrictInt = Field(default=50, ge=1, le=100)
    max_depth: StrictInt = Field(default=4, ge=0, le=8)

    @model_validator(mode="after")
    def pattern_safe(self):
        if any(char in self.pattern for char in '/\\:\x00') or any(ord(char)<32 for char in self.pattern):
            raise ValueError("filename pattern required")
        return self


class FilePreviewArguments(StrictContract):
    root_id: Literal["workspace"]
    path: StrictStr = Field(min_length=1, max_length=240)
    content: StrictStr = Field(max_length=12000)

    @model_validator(mode="after")
    def bounded(self):
        relative_path(self.path)
        if len(self.content.encode("utf-8")) > 12000 or "\x00" in self.content:
            raise ValueError("bounded UTF-8 text required")
        return self


class FileWriteArguments(FilePreviewArguments):
    expected_sha256: CommandDigest | None
    preview_task_id: UUID
    preview_digest: CommandDigest


class CommandRunArguments(StrictContract):
    root_id: Literal["workspace"]
    command_id: CommandId
    template_digest: CommandDigest


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_digest(root_id, path, previous, content_sha256):
    import json
    return hashlib.sha256(json.dumps([root_id, path, previous, content_sha256], separators=(",", ":")).encode()).hexdigest()
