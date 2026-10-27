from nexus.contracts.android_screen import AndroidScreenObserveArguments, AndroidScreenActArguments, AndroidScreenResult, AndroidScreenObserveCall, AndroidScreenActCall
from nexus.contracts.android_control import (
    AndroidUIObserveArguments,
    AndroidUIObserveCall,
    AndroidUIResult,
    AndroidUIActArguments,
    AndroidUIActCall,
    AndroidFilesArguments,
    AndroidFilesCall,
    AndroidFilesResult,
    AndroidFilePreviewArguments,
    AndroidFilePreviewCall,
    AndroidFilePreviewResult,
    AndroidFileWriteArguments,
    AndroidFileWriteCall,
    AndroidFileWriteResult,
)
from nexus.contracts.file_transfer import FileReceiveArguments, FileReceiveCall, FileReceiveResult
from nexus.plugins.contracts import PluginNodeArguments, PluginNodeResult, PluginNodeCall

from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator
from nexus.contracts.basic import FileReadArguments, FileSearchArguments, FilePreviewArguments, FileWriteArguments, CommandRunArguments, FileLinesArguments, FileGlobArguments, FileGrepArguments, FileEditPreviewArguments, FileEditArguments, glob_matches, relative_path, text_digest, write_digest
from nexus.contracts.interaction import ObserveArguments, InteractArguments, InteractionResult
from nexus.contracts.images import ImageReadArguments, ImageReadResult
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


class TextLine(StrictContract):
    number: StrictInt = Field(ge=1)
    text: StrictStr
    truncated: StrictBool = False


class FileLinesResult(ToolResultContract):
    root_id: Literal["workspace"]
    path: StrictStr
    offset: StrictInt = Field(ge=1)
    lines: list[TextLine] = Field(max_length=200)
    total_lines: StrictInt = Field(ge=0)
    next_offset: StrictInt | None
    sha256: CommandDigest
    size_bytes: StrictInt = Field(ge=0, le=1048576)
    truncated: StrictBool


class FileGlobResult(FileSearchResult):
    base_path: StrictStr


class GrepMatch(TextLine):
    path: StrictStr


class FileGrepResult(ToolResultContract):
    root_id: Literal["workspace"]
    matches: list[GrepMatch] = Field(max_length=100)
    scanned: StrictInt = Field(ge=0, le=2000)
    skipped: StrictInt = Field(ge=0, le=2000)
    bytes_scanned: StrictInt = Field(ge=0, le=4194304)
    truncated: StrictBool


class FileEditPreviewResult(FilePreviewResult):
    replacements: StrictInt = Field(ge=1, le=12000)
    bytes_written: StrictInt = Field(ge=0, le=12000)


class FileEditResult(FileWriteResult):
    replacements: StrictInt = Field(ge=1, le=12000)


class FileReadCall(StrictContract):
    tool: Literal["filesystem.read"]
    tool_version: Version1
    arguments: FileReadArguments


class ImageReadCall(StrictContract):
    tool: Literal["filesystem.read_image"]
    tool_version: Version1
    arguments: ImageReadArguments


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


class FileLinesCall(StrictContract):
    tool: Literal["filesystem.read_lines"]
    tool_version: Version1
    arguments: FileLinesArguments


class FileGlobCall(StrictContract):
    tool: Literal["filesystem.glob"]
    tool_version: Version1
    arguments: FileGlobArguments


class FileGrepCall(StrictContract):
    tool: Literal["filesystem.grep"]
    tool_version: Version1
    arguments: FileGrepArguments


class FileEditPreviewCall(StrictContract):
    tool: Literal["filesystem.edit_preview"]
    tool_version: Version1
    arguments: FileEditPreviewArguments


class FileEditCall(StrictContract):
    tool: Literal["filesystem.edit"]
    tool_version: Version1
    arguments: FileEditArguments


class ObserveCall(StrictContract):
    tool: Literal["interaction.observe"]
    tool_version: Version1
    arguments: ObserveArguments


class InteractCall(StrictContract):
    tool: Literal["interaction.act"]
    tool_version: Version1
    arguments: InteractArguments


from nexus.contracts.android import AndroidDeviceStatusArguments, AndroidNotificationDigestArguments, AndroidDeviceStatusResult, AndroidNotificationDigestResult, AndroidDeviceStatusCall, AndroidNotificationDigestCall

ToolCall = Annotated[AndroidScreenObserveCall | AndroidScreenActCall | AndroidUIObserveCall | AndroidUIActCall | AndroidFilesCall | AndroidFilePreviewCall | AndroidFileWriteCall | SystemInfoCall | ListDirectoryCall | FileReadCall | FileSearchCall | FilePreviewCall | FileWriteCall | CommandRunCall | FileLinesCall | FileGlobCall | FileGrepCall | FileEditPreviewCall | FileEditCall | ObserveCall | InteractCall | ImageReadCall | PluginNodeCall | AndroidDeviceStatusCall | AndroidNotificationDigestCall | FileReceiveCall, Field(discriminator="tool")]
ToolResult = AndroidScreenResult | AndroidUIResult | AndroidFilesResult | AndroidFilePreviewResult | AndroidFileWriteResult | InteractionResult | SystemInfoResult | ListDirectoryResult | FileReadResult | FileSearchResult | FilePreviewResult | FileWriteResult | CommandRunResult | FileLinesResult | FileGlobResult | FileGrepResult | FileEditPreviewResult | FileEditResult | ImageReadResult | PluginNodeResult | AndroidDeviceStatusResult | AndroidNotificationDigestResult | FileReceiveResult


TOOL_ARGUMENT_MODELS = {
    "android.screen_observe@1": AndroidScreenObserveArguments,
    "android.ui_observe@1": AndroidUIObserveArguments,
    "android.screen_act@1": AndroidScreenActArguments,
    "android.ui_act@1": AndroidUIActArguments,
    "android.files@1": AndroidFilesArguments,
    "android.file_preview@1": AndroidFilePreviewArguments,
    "android.file_write@1": AndroidFileWriteArguments,

    "filesystem.receive@1": FileReceiveArguments,
    "android.device_status@1": AndroidDeviceStatusArguments,
    "android.notification_digest@1": AndroidNotificationDigestArguments,
    "plugin.call@1": PluginNodeArguments,
    "filesystem.read_image@1": ImageReadArguments,
    "interaction.observe@1": ObserveArguments,
    "interaction.act@1": InteractArguments,
    "system.info@1": SystemInfoArguments,
    "filesystem.list@1": ListDirectoryArguments,
    "filesystem.read@1": FileReadArguments,
    "filesystem.search@1": FileSearchArguments,
    "filesystem.write_preview@1": FilePreviewArguments,
    "filesystem.write@1": FileWriteArguments,
    "command.run@1": CommandRunArguments,
    "filesystem.read_lines@1": FileLinesArguments,
    "filesystem.glob@1": FileGlobArguments,
    "filesystem.grep@1": FileGrepArguments,
    "filesystem.edit_preview@1": FileEditPreviewArguments,
    "filesystem.edit@1": FileEditArguments,
}

TOOL_RESULT_MODELS = {
    "android.screen_observe@1": AndroidScreenResult,
    "android.ui_observe@1": AndroidUIResult,
    "android.screen_act@1": AndroidScreenResult,
    "android.ui_act@1": AndroidUIResult,
    "android.files@1": AndroidFilesResult,
    "android.file_preview@1": AndroidFilePreviewResult,
    "android.file_write@1": AndroidFileWriteResult,

    "filesystem.receive@1": FileReceiveResult,
    "android.device_status@1": AndroidDeviceStatusResult,
    "android.notification_digest@1": AndroidNotificationDigestResult,
    "plugin.call@1": PluginNodeResult,
    "filesystem.read_image@1": ImageReadResult,
    "interaction.observe@1": InteractionResult,
    "interaction.act@1": InteractionResult,
    "system.info@1": SystemInfoResult,
    "filesystem.list@1": ListDirectoryResult,
    "filesystem.read@1": FileReadResult,
    "filesystem.search@1": FileSearchResult,
    "filesystem.write_preview@1": FilePreviewResult,
    "filesystem.write@1": FileWriteResult,
    "command.run@1": CommandRunResult,
    "filesystem.read_lines@1": FileLinesResult,
    "filesystem.glob@1": FileGlobResult,
    "filesystem.grep@1": FileGrepResult,
    "filesystem.edit_preview@1": FileEditPreviewResult,
    "filesystem.edit@1": FileEditResult,
}


def validate_result(command: ToolCall, result: ToolResult, max_bytes: int) -> ToolResult:
    """Shared schema/constraint validation; no platform or authority imports."""
    model = TOOL_RESULT_MODELS[f"{command.tool}@{command.tool_version}"]
    checked = model.model_validate_json(result.model_dump_json())
    args = command.arguments
    if command.tool.startswith("android."):
        from nexus.contracts.android_control import validate_android_result
        validate_android_result(command, checked)
        from nexus.contracts.android_screen import validate_screen_result
        validate_screen_result(command, checked)
    if isinstance(checked, FileReceiveResult) and (checked.attachment_id != args.attachment_id or checked.sha256 != args.sha256 or checked.bytes_written != args.size_bytes):
        raise ValueError("file delivery evidence mismatch")
    if isinstance(checked, AndroidNotificationDigestResult) and len(checked.entries) > args.limit:
        raise ValueError("notification result exceeds requested limit")
    if isinstance(checked, PluginNodeResult) and (checked.release_digest != args.release_digest or checked.tool != args.tool):
        raise ValueError("plugin result identity mismatch")
    if isinstance(checked, PluginNodeResult) and args.plugin_id == 'builtin.android_emulator' and args.tool == 'observe':
        from nexus.contracts.images import ImageSnapshot
        ImageSnapshot.model_validate({key: checked.data[key] for key in ImageSnapshot.model_fields})
        import re
        if not re.fullmatch('[a-f0-9]{64}', checked.data.get('observation_digest', '')):
            raise ValueError('invalid emulator observation identity')
    if hasattr(checked, "root_id") and checked.root_id != args.root_id:
        raise ValueError("resource mismatch")
    if hasattr(checked, "path") and checked.path != args.path:
        raise ValueError("path mismatch")
    if isinstance(checked, ImageReadResult) and max(checked.image.width, checked.image.height) > args.max_dimension:
        raise ValueError("image exceeds requested dimensions")
    if isinstance(checked, InteractionResult):
        import hashlib
        import json
        observation=checked.observation
        if checked.target_id!=args.target_id or checked.template_digest!=args.template_digest or hashlib.sha256(observation.state_json.encode()).hexdigest()!=observation.state_sha256 or not isinstance(json.loads(observation.state_json),dict):
            raise ValueError("interaction observation mismatch")
        if command.tool=="interaction.act":
            if checked.action_id!=args.action_id or checked.previous_state_sha256!=args.expected_state_sha256 or checked.verification!="configured_action_returned":
                raise ValueError("interaction action evidence mismatch")
        elif checked.action_id is not None or checked.previous_state_sha256 is not None or checked.verification!="configured_target_observed":
            raise ValueError("observation must not claim an action")
    if isinstance(checked, FileReadResult):
        size = len(checked.text.encode())
        if size > args.max_bytes or size > checked.size_bytes or (not checked.truncated and (size != checked.size_bytes or text_digest(checked.text) != checked.sha256)):
            raise ValueError("invalid read evidence")
    if isinstance(checked, FileSearchResult) and not isinstance(checked, FileGlobResult):
        if len(set(checked.paths)) != len(checked.paths) or len(checked.paths) > args.limit:
            raise ValueError("invalid search evidence")
        import fnmatch
        for path in checked.paths:
            relative_path(path)
            if len(path.split("/"))-1 > args.max_depth or not fnmatch.fnmatchcase(path.rsplit("/",1)[-1].casefold(), args.pattern.casefold()):
                raise ValueError("search result outside query")
    if isinstance(checked, FilePreviewResult) and not isinstance(checked, FileEditPreviewResult):
        if checked.content_sha256 != text_digest(args.content) or checked.preview_digest != write_digest(args.root_id,args.path,checked.previous_sha256,checked.content_sha256,args.access_root):
            raise ValueError("preview mismatch")
    if isinstance(checked, FileWriteResult) and not isinstance(checked, FileEditResult):
        if checked.sha256 != text_digest(args.content) or checked.bytes_written != len(args.content.encode()):
            raise ValueError("write evidence mismatch")
    if isinstance(checked, FileLinesResult):
        numbers = [line.number for line in checked.lines]
        end = numbers[-1] if numbers else args.offset-1
        if (checked.offset != args.offset or len(checked.lines) > args.limit
                or numbers != list(range(args.offset,args.offset+len(numbers))) or end > max(checked.total_lines,args.offset-1)
                or sum(len(line.text.encode()) for line in checked.lines) > args.max_bytes
                or checked.next_offset != (end+1 if end < checked.total_lines else None)
                or checked.truncated != (checked.next_offset is not None or any(line.truncated for line in checked.lines))):
            raise ValueError("line window evidence mismatch")
    if isinstance(checked, FileGlobResult):
        if checked.base_path != args.path or len(set(checked.paths)) != len(checked.paths) or len(checked.paths) > args.limit:
            raise ValueError("glob evidence mismatch")
        for path in checked.paths:
            relative_path(path)
            relative = path if args.path == "." else path.removeprefix(args.path+"/")
            if (args.path != "." and relative == path or len(relative.split("/"))-1 > args.max_depth or not glob_matches(relative,args.pattern)):
                raise ValueError("glob path outside query")
    if isinstance(checked, FileGrepResult):
        if len(checked.matches) > args.limit or len({(m.path,m.number) for m in checked.matches}) != len(checked.matches):
            raise ValueError("grep evidence mismatch")
        for item in checked.matches:
            relative_path(item.path)
            relative = item.path if args.path == "." else item.path.removeprefix(args.path+"/")
            if (args.path != "." and relative == item.path or len(relative.split("/"))-1 > args.max_depth
                    or not glob_matches(relative,args.include) or len(item.text.encode()) > 1024
                    or not item.truncated and (args.pattern.casefold() if args.ignore_case else args.pattern) not in (item.text.casefold() if args.ignore_case else item.text)):
                raise ValueError("grep match outside query")
    if isinstance(checked, FileEditPreviewResult):
        if (checked.previous_sha256 != args.expected_sha256 or not args.replace_all and checked.replacements != 1
                or checked.preview_digest != write_digest(args.root_id,args.path,args.expected_sha256,checked.content_sha256,args.access_root)):
            raise ValueError("edit preview mismatch")
    if isinstance(checked, FileEditResult):
        if (checked.sha256 != args.content_sha256 or checked.bytes_written != args.bytes_written
                or not args.replace_all and checked.replacements != 1):
            raise ValueError("edit evidence mismatch")
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
