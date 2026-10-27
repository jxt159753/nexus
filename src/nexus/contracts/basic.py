"""Bounded, explicit workspace operations; no arbitrary paths or shell strings."""

from nexus.contracts.host_access import HostAccess
import hashlib
from typing import Annotated, Literal, get_args
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from nexus.contracts.base import CommandDigest, StrictContract

ToolName = Literal["system.info", "filesystem.list", "filesystem.read", "filesystem.search", "filesystem.write_preview", "filesystem.write", "command.run", "filesystem.read_lines", "filesystem.glob", "filesystem.grep", "filesystem.edit_preview", "filesystem.edit", "interaction.observe", "interaction.act", "filesystem.read_image", "plugin.call", "android.device_status", "android.notification_digest", "filesystem.receive", "android.ui_observe", "android.ui_act", "android.screen_observe", "android.screen_act", "android.files", "android.file_preview", "android.file_write"]
ToolKey = Literal["system.info@1", "filesystem.list@1", "filesystem.read@1", "filesystem.search@1", "filesystem.write_preview@1", "filesystem.write@1", "command.run@1", "filesystem.read_lines@1", "filesystem.glob@1", "filesystem.grep@1", "filesystem.edit_preview@1", "filesystem.edit@1", "interaction.observe@1", "interaction.act@1", "filesystem.read_image@1", "plugin.call@1", "android.device_status@1", "android.notification_digest@1", "filesystem.receive@1", "android.ui_observe@1", "android.ui_act@1", "android.screen_observe@1", "android.screen_act@1", "android.files@1", "android.file_preview@1", "android.file_write@1"]
ResourceId = Literal["workspace", "android:device", "android:notifications", "android:ui", "android:files"]
TOOL_RESOURCE = {name: "workspace" for name in get_args(ToolName)}
TOOL_RESOURCE.update({"system.info": "", "android.device_status": "android:device", "android.notification_digest": "android:notifications", "android.ui_observe": "android:ui", "android.ui_act": "android:ui", "android.screen_observe": "android:ui", "android.screen_act": "android:ui", "android.files": "android:files", "android.file_preview": "android:files", "android.file_write": "android:files"})

MUTATING_TOOLS = frozenset({"android.ui_act", "android.screen_act", "android.file_write", "filesystem.receive", "filesystem.write", "filesystem.edit", "command.run", "interaction.act", "plugin.call"})
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


class FileReadArguments(HostAccess):
    root_id: Literal["workspace"]
    path: StrictStr = Field(min_length=1, max_length=240)
    max_bytes: StrictInt = Field(default=8192, ge=1, le=8192)

    @model_validator(mode="after")
    def path_safe(self):
        relative_path(self.path)
        return self


class FileLinesArguments(FileReadArguments):
    offset: StrictInt = Field(default=1, ge=1, le=1_000_000)
    limit: StrictInt = Field(default=100, ge=1, le=200)


def glob_pattern(value: str) -> str:
    if (not value.strip() or len(value.encode()) > 300 or value.startswith("/")
            or any(ord(c) < 32 or c in '\\:<>|"!{}' for c in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("bounded relative glob required; supports *, **, ? and character classes")
    return value


def glob_matches(path: str, pattern: str) -> bool:
    import fnmatch
    from functools import cache
    path_parts, pattern_parts = path.casefold().split("/"), pattern.casefold().split("/")
    if len(pattern_parts) == 1:
        return fnmatch.fnmatchcase(path_parts[-1], pattern_parts[0])

    @cache
    def match(i, j):
        if j == len(pattern_parts):
            return i == len(path_parts)
        if pattern_parts[j] == "**":
            return match(i, j+1) or i < len(path_parts) and match(i+1, j)
        return i < len(path_parts) and fnmatch.fnmatchcase(path_parts[i], pattern_parts[j]) and match(i+1, j+1)
    return match(0, 0)


class FileGlobArguments(HostAccess):
    root_id: Literal["workspace"]
    path: StrictStr = Field(default=".", min_length=1, max_length=240)
    pattern: StrictStr = Field(min_length=1, max_length=100)
    limit: StrictInt = Field(default=50, ge=1, le=100)
    max_depth: StrictInt = Field(default=4, ge=0, le=8)

    @model_validator(mode="after")
    def safe(self):
        if self.path != ".":
            relative_path(self.path)
        glob_pattern(self.pattern)
        return self


class FileGrepArguments(HostAccess):
    root_id: Literal["workspace"]
    path: StrictStr = Field(default=".", min_length=1, max_length=240)
    pattern: StrictStr = Field(min_length=1, max_length=200, description="逐行查找的字面文本，不是正则表达式。")
    include: StrictStr = Field(default="*", min_length=1, max_length=100)
    ignore_case: StrictBool = False
    limit: StrictInt = Field(default=50, ge=1, le=100)
    max_depth: StrictInt = Field(default=4, ge=0, le=8)

    @model_validator(mode="after")
    def safe(self):
        if self.path != ".":
            relative_path(self.path)
        glob_pattern(self.include)
        if not self.pattern.strip() or any(ord(c) < 32 for c in self.pattern):
            raise ValueError("one nonempty line of literal search text required")
        return self


class FileSearchArguments(HostAccess):
    root_id: Literal["workspace"]
    pattern: StrictStr = Field(min_length=1, max_length=100)
    limit: StrictInt = Field(default=50, ge=1, le=100)
    max_depth: StrictInt = Field(default=4, ge=0, le=8)

    @model_validator(mode="after")
    def pattern_safe(self):
        if any(char in self.pattern for char in '/\\:\x00') or any(ord(char)<32 for char in self.pattern):
            raise ValueError("filename pattern required")
        return self


class FilePreviewArguments(HostAccess):
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


class FileEditPreviewArguments(HostAccess):
    root_id: Literal["workspace"]
    path: StrictStr = Field(min_length=1, max_length=240)
    old_string: StrictStr = Field(min_length=1, max_length=12000)
    new_string: StrictStr = Field(max_length=12000)
    replace_all: StrictBool = False
    expected_sha256: CommandDigest

    @model_validator(mode="after")
    def safe(self):
        relative_path(self.path)
        if (self.old_string == self.new_string or any("\x00" in text or len(text.encode()) > 12000 for text in (self.old_string, self.new_string))):
            raise ValueError("distinct bounded UTF-8 replacements required")
        return self


class FileEditArguments(FileEditPreviewArguments):
    preview_task_id: UUID
    preview_digest: CommandDigest
    content_sha256: CommandDigest
    bytes_written: StrictInt = Field(ge=0, le=12000)


class CommandRunArguments(HostAccess):
    root_id: Literal["workspace"]
    command_id: CommandId
    template_digest: CommandDigest


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_digest(root_id, path, previous, content_sha256, access_root=None):
    import json
    scope = [root_id, path, previous, content_sha256]
    if access_root is not None:
        scope.append(access_root)
    return hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
