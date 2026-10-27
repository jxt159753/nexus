
from dataclasses import dataclass
from typing import Literal

from nexus.contracts.errors import ErrorCode
from nexus.contracts.task import (
    ListDirectoryRequest,
    SystemInfoRequest,
    TaskRequest,
)


RoutingMode = Literal["rule", "reject"]


@dataclass(frozen=True)
class RoutingDecision:
    mode: RoutingMode
    reason_code: str
    policy_version: int
    tool_name: str | None
    tool_version: int | None
    arguments: dict[str, object] | None
    error_code: ErrorCode | None = None


def route_task(
    request: TaskRequest,
) -> RoutingDecision:
    tools = {"android_screen_observe":"android.screen_observe","android_screen_act":"android.screen_act","android_ui_observe":"android.ui_observe","android_ui_act":"android.ui_act","android_files":"android.files","android_file_preview":"android.file_preview","android_file_write":"android.file_write","receive_file":"filesystem.receive", "android_device_status":"android.device_status", "android_notification_digest":"android.notification_digest", "execute_plugin":"plugin.call","read_image":"filesystem.read_image","observe_target":"interaction.observe","interact_target":"interaction.act","read_file_lines": "filesystem.read_lines", "glob_files": "filesystem.glob", "search_file_content": "filesystem.grep", "preview_file_edit": "filesystem.edit_preview", "edit_file": "filesystem.edit", "read_file": "filesystem.read", "find_files": "filesystem.search", "preview_file_write": "filesystem.write_preview", "write_file": "filesystem.write", "run_command": "command.run"}
    if getattr(request, "intent", None) in tools:
        return RoutingDecision("rule", "KNOWN_WORKSPACE_TOOL", 1, tools[request.intent], 1, request.arguments.model_dump(mode="json"))
    if isinstance(request, SystemInfoRequest):
        return RoutingDecision(
            mode="rule",
            reason_code="KNOWN_SYSTEM_INFO",
            policy_version=1,
            tool_name="system.info",
            tool_version=1,
            arguments=request.arguments.model_dump(),
        )

    if isinstance(request, ListDirectoryRequest):
        return RoutingDecision(
            mode="rule",
            reason_code="KNOWN_DIRECTORY_LIST",
            policy_version=1,
            tool_name="filesystem.list",
            tool_version=1,
            arguments=request.arguments.model_dump(),
        )

    return RoutingDecision(
        mode="reject",
        reason_code="UNKNOWN_INTENT",
        policy_version=1,
        tool_name=None,
        tool_version=None,
        arguments=None,
        error_code=ErrorCode.UNSUPPORTED_TASK,
    )
