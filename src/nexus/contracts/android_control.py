"""Phone UI and user-selected document trees; no Android or Core imports."""

import hashlib
import json
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from nexus.contracts.base import CommandDigest, StrictContract, Version1


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def document_path(value: str, *, directory: bool = False) -> str:
    if directory and value == "":
        return value
    if (not value or len(value.encode()) > 512 or any(c in value for c in "\\\x00")
            or any(ord(c) < 32 for c in value) or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise ValueError("selected-tree relative path required")
    return value


class AndroidUIObserveArguments(StrictContract):
    pass


class AndroidUIActArguments(StrictContract):
    observation_digest: CommandDigest
    action: Literal["click", "long_click", "set_text", "scroll_forward", "scroll_backward", "back", "home", "recents"]
    node: StrictStr | None = Field(default=None, max_length=160, pattern=r"^r(?:\.[0-9]+)*$")
    text: StrictStr | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.node is None) != (self.action in {"back", "home", "recents"}):
            raise ValueError("node is required only for node actions")
        if (self.text is not None) != (self.action == "set_text"):
            raise ValueError("text is required only for set_text")
        return self


class AndroidUINode(StrictContract):
    node: StrictStr = Field(max_length=160, pattern=r"^r(?:\.[0-9]+)*$")
    text: StrictStr = Field(max_length=300)
    description: StrictStr = Field(max_length=300)
    class_name: StrictStr = Field(max_length=200)
    bounds: list[StrictInt] = Field(min_length=4, max_length=4)
    actions: list[Literal["click", "long_click", "set_text", "scroll_forward", "scroll_backward"]] = Field(max_length=5)
    password: StrictBool


class AndroidUIState(StrictContract):
    package_name: StrictStr = Field(min_length=1, max_length=200)
    window_id: StrictInt
    nodes: list[AndroidUINode] = Field(max_length=100)
    truncated: StrictBool

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({item.node for item in self.nodes}) != len(self.nodes):
            raise ValueError("duplicate UI node")
        if any(item.password and (item.text or item.description or item.actions) for item in self.nodes):
            raise ValueError("password controls must be redacted")
        return self


class AndroidUIResult(StrictContract):
    state: AndroidUIState
    observation_digest: CommandDigest
    action: StrictStr | None = None
    previous_digest: CommandDigest | None = None
    verification: Literal["ui_observed", "action_returned"]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if digest(self.state.model_dump(mode="json")) != self.observation_digest:
            raise ValueError("UI observation digest mismatch")
        if (self.action is None) != (self.verification == "ui_observed") or (self.previous_digest is None) != (self.action is None):
            raise ValueError("UI action evidence mismatch")
        return self


class AndroidFilesArguments(StrictContract):
    operation: Literal["roots", "list", "read"]
    tree_id: CommandDigest | None = None
    path: StrictStr = Field(default="", max_length=512)
    offset: StrictInt = Field(default=0, ge=0, le=1_048_576)
    limit: StrictInt = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.operation == "roots":
            if self.tree_id is not None or self.path or self.offset:
                raise ValueError("roots does not take a tree, path or offset")
        else:
            if self.tree_id is None:
                raise ValueError("choose a granted tree first")
            document_path(self.path, directory=self.operation == "list")
        return self


class AndroidFileEntry(StrictContract):
    name: StrictStr = Field(min_length=1, max_length=255)
    kind: Literal["file", "directory"]
    tree_id: CommandDigest | None = None
    writable: StrictBool


class AndroidFilesResult(StrictContract):
    operation: Literal["roots", "list", "read"]
    tree_id: CommandDigest | None
    path: StrictStr
    offset: StrictInt = Field(ge=0)
    entries: list[AndroidFileEntry] = Field(max_length=100)
    text: StrictStr | None
    sha256: CommandDigest | None
    size_bytes: StrictInt | None = Field(ge=0, le=1_048_576)
    next_offset: StrictInt | None = Field(ge=1)
    truncated: StrictBool


class AndroidFilePreviewArguments(StrictContract):
    tree_id: CommandDigest
    path: StrictStr = Field(min_length=1, max_length=512)
    content: StrictStr = Field(max_length=12000)

    @model_validator(mode="after")
    def safe(self) -> Self:
        document_path(self.path)
        if len(self.content.encode()) > 12000:
            raise ValueError("content exceeds 12000 UTF-8 bytes")
        return self


class AndroidFileWriteArguments(AndroidFilePreviewArguments):
    expected_sha256: CommandDigest | None
    preview_task_id: UUID
    preview_digest: CommandDigest


def preview_digest(tree_id: str, path: str, previous: str | None, content: str) -> str:
    return digest({"tree_id": tree_id, "path": path, "previous_sha256": previous, "content_sha256": content})


class AndroidFilePreviewResult(StrictContract):
    tree_id: CommandDigest
    path: StrictStr
    previous_sha256: CommandDigest | None
    content_sha256: CommandDigest
    preview_digest: CommandDigest
    previous_text: StrictStr | None = Field(max_length=12000)
    bytes_written: StrictInt = Field(ge=0, le=12000)


class AndroidFileWriteResult(StrictContract):
    tree_id: CommandDigest
    path: StrictStr
    sha256: CommandDigest
    bytes_written: StrictInt = Field(ge=0, le=12000)
    backup_path: StrictStr | None = Field(max_length=600)


class AndroidUIObserveCall(StrictContract):
    tool: Literal["android.ui_observe"]
    tool_version: Version1
    arguments: AndroidUIObserveArguments


class AndroidUIActCall(StrictContract):
    tool: Literal["android.ui_act"]
    tool_version: Version1
    arguments: AndroidUIActArguments


class AndroidFilesCall(StrictContract):
    tool: Literal["android.files"]
    tool_version: Version1
    arguments: AndroidFilesArguments


class AndroidFilePreviewCall(StrictContract):
    tool: Literal["android.file_preview"]
    tool_version: Version1
    arguments: AndroidFilePreviewArguments


class AndroidFileWriteCall(StrictContract):
    tool: Literal["android.file_write"]
    tool_version: Version1
    arguments: AndroidFileWriteArguments


def validate_android_result(command, result) -> None:
    args = command.arguments
    if isinstance(result, AndroidUIResult):
        if command.tool == "android.ui_act":
            if result.action != args.action or result.previous_digest != args.observation_digest or result.verification != "action_returned":
                raise ValueError("UI action does not match command")
        elif result.verification != "ui_observed":
            raise ValueError("read cannot claim action")
    if isinstance(result, (AndroidFilesResult, AndroidFilePreviewResult, AndroidFileWriteResult)):
        if result.tree_id != args.tree_id or result.path != args.path:
            raise ValueError("selected document mismatch")
    if isinstance(result, AndroidFilesResult):
        if result.operation != args.operation or result.offset != args.offset or result.truncated != (result.next_offset is not None):
            raise ValueError("file query mismatch")
        if args.operation == "read":
            if result.entries or result.text is None or result.sha256 is None or result.size_bytes is None or len(result.text.encode()) > 8192:
                raise ValueError("invalid file read evidence")
            if result.next_offset is not None and (not result.text or result.next_offset != args.offset + len(result.text)):
                raise ValueError("invalid text continuation")
            if args.offset == 0 and not result.truncated and (len(result.text.encode()) != result.size_bytes or hashlib.sha256(result.text.encode()).hexdigest() != result.sha256):
                raise ValueError("file hash mismatch")
        else:
            if result.text is not None or result.sha256 is not None or result.size_bytes is not None or len(result.entries) > args.limit:
                raise ValueError("invalid listing evidence")
            if result.next_offset is not None and (not result.entries or result.next_offset != args.offset + len(result.entries)):
                raise ValueError("invalid listing continuation")
            identifiers = [entry.tree_id if args.operation == "roots" else entry.name for entry in result.entries]
            if len(set(identifiers)) != len(identifiers) or None in identifiers:
                raise ValueError("duplicate or absent directory identity")
            for entry in result.entries:
                if args.operation == "list":
                    document_path(entry.name)
                    if "/" in entry.name or entry.tree_id is not None:
                        raise ValueError("invalid direct child")
    if isinstance(result, (AndroidFilePreviewResult, AndroidFileWriteResult)):
        sha = hashlib.sha256(args.content.encode()).hexdigest()
        if result.bytes_written != len(args.content.encode()):
            raise ValueError("written size mismatch")
        if isinstance(result, AndroidFilePreviewResult):
            if (result.content_sha256 != sha or result.preview_digest != preview_digest(args.tree_id, args.path, result.previous_sha256, sha)
                    or (result.previous_text is None) != (result.previous_sha256 is None)
                    or result.previous_text is not None and hashlib.sha256(result.previous_text.encode()).hexdigest() != result.previous_sha256):
                raise ValueError("document preview mismatch")
        elif result.sha256 != sha or (result.backup_path is None) != (args.expected_sha256 is None):
            raise ValueError("document write evidence mismatch")
        elif result.backup_path is not None:
            document_path(result.backup_path)
            if result.backup_path.rpartition('/')[0] != args.path.rpartition('/')[0] or result.backup_path == args.path:
                raise ValueError("backup must be a separate sibling document")
