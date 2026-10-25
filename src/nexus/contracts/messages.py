from nexus.contracts.file_transfer import FileReceiveCall, FileReceiveResult
from nexus.plugins.contracts import PluginNodeCall, PluginNodeResult,PluginId,ToolId
"""JARVIS Link v1 payloads. These types do not authorize or execute actions."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from nexus.contracts.base import (
    MAX_MESSAGE_BYTES,
    MAX_RESULT_BYTES,
    CommandDigest,
    EventId,
    Revision,
    StrictContract,
    UTCDateTime,
    Version1,
)
from nexus.contracts.errors import ErrorPayload
from nexus.contracts.basic import ToolName, CommandId, ResourceId
from nexus.contracts.android import AndroidDeviceStatusArguments, AndroidNotificationDigestArguments, AndroidDeviceStatusResult, AndroidNotificationDigestResult, AndroidDeviceStatusCall, AndroidNotificationDigestCall

from nexus.contracts.interaction import InteractionResult
from nexus.contracts.tools import ImageReadCall, ImageReadResult
from nexus.contracts.tools import ObserveCall, InteractCall, FileLinesCall, FileLinesResult, FileGlobCall, FileGlobResult, FileGrepCall, FileGrepResult, FileEditPreviewCall, FileEditPreviewResult, FileEditCall, FileEditResult, FileReadCall, FileSearchCall, FilePreviewCall, FileWriteCall, CommandRunCall, FileReadResult, FileSearchResult, FilePreviewResult, FileWriteResult, CommandRunResult
from nexus.contracts.task import ExecutionOutcome, TaskStatus
from nexus.contracts.tools import (
    ListDirectoryCall,
    ListDirectoryResult,
    SystemInfoCall,
    SystemInfoResult,
    ToolCall,
    ToolResult,
)


class ClientSync(StrictContract):
    event_id: EventId | None


class ClientSynced(StrictContract):
    event_id: EventId
    full_sync_required: StrictBool


class TaskCancel(StrictContract):
    task_id: UUID


class ApprovalDecide(StrictContract):
    approval_id: UUID
    decision: Literal["approve", "reject"]


class ControlRequest(StrictContract):
    request_id: UUID


class TaskAccepted(StrictContract):
    task_id: UUID
    status: TaskStatus


class TaskUpdated(StrictContract):
    event_id: EventId
    task_id: UUID
    revision: Revision
    status: TaskStatus
    execution_outcome: ExecutionOutcome
    result: ToolResult | None = None
    error: ErrorPayload | None = None
    verification: Literal["node_report_schema_checked"] | None = None

    @model_validator(mode="after")
    def check_evidence_shape(self) -> Self:
        if self.result is not None and self.error is not None:
            raise ValueError("result and error are mutually exclusive")
        if self.status == "succeeded" and (
            self.result is None or self.execution_outcome != "known"
            or self.verification is None
        ):
            raise ValueError("succeeded requires known result and verification level")
        return self


class ApprovalRequired(StrictContract):
    event_id: EventId
    approval_id: UUID
    task_id: UUID
    node_id: UUID
    action: ToolCall
    expires_at: UTCDateTime


class ControlUpdated(StrictContract):
    event_id: EventId
    paused: StrictBool
    revision: Revision


class ToolCapability(StrictContract):
    tool: ToolName
    tool_version: Version1


class NodeHello(StrictContract):
    mcp_execution_timeout_seconds: StrictInt = Field(default=10, ge=1, le=60)
    plugin_digest: CommandDigest | None=None
    plugin_tools: dict[PluginId,list[ToolId]]=Field(default_factory=dict,max_length=32)
    node_id: UUID
    node_version: StrictStr = Field(min_length=1, max_length=50)
    tools: list[ToolCapability] = Field(max_length=20)
    interaction_digests: dict[CommandId, CommandDigest] = Field(default_factory=dict, max_length=16)
    command_digests: dict[CommandId, CommandDigest] = Field(default_factory=dict, max_length=16)
    roots: list[ResourceId] = Field(max_length=3)
    boot_id: UUID

    @model_validator(mode="after")
    def check_unique_capabilities(self) -> Self:
        keys = [(item.tool, item.tool_version) for item in self.tools]
        if len(set(keys)) != len(keys) or len(set(self.roots)) != len(self.roots):
            raise ValueError("duplicate tool capability")
        return self


class LinkLimits(StrictContract):
    max_message_bytes: StrictInt = Field(ge=1, le=MAX_MESSAGE_BYTES)
    max_result_bytes: StrictInt = Field(ge=1, le=MAX_RESULT_BYTES)
    execution_timeout_seconds: StrictInt = Field(ge=1, le=10)
    max_concurrent_attempts: Annotated[StrictInt, Field(ge=1, le=1)]

    @model_validator(mode="after")
    def check_result_limit(self) -> Self:
        if self.max_result_bytes >= self.max_message_bytes:
            raise ValueError("result limit must leave space for message metadata")
        return self


class SessionPayload(StrictContract):
    session_id: UUID


class NodeWelcome(SessionPayload):
    heartbeat_interval_seconds: StrictInt = Field(ge=1, le=10)
    heartbeat_timeout_seconds: StrictInt = Field(ge=1, le=30)
    limits: LinkLimits

    @model_validator(mode="after")
    def check_heartbeat_window(self) -> Self:
        if self.heartbeat_timeout_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("heartbeat timeout must exceed interval")
        return self


class NodeHeartbeat(SessionPayload):
    active_attempt_id: UUID | None


class CommandExecuteBase(SessionPayload):
    task_id: UUID
    attempt_id: UUID
    command_digest: CommandDigest
    grant_id: UUID
    expires_at: UTCDateTime
    execution_timeout_seconds: StrictInt = Field(ge=1, le=10)


class SystemInfoExecute(CommandExecuteBase, SystemInfoCall):
    pass


class ListDirectoryExecute(CommandExecuteBase, ListDirectoryCall):
    pass


class FileReadExecute(CommandExecuteBase, FileReadCall):
    pass


class ImageReadExecute(CommandExecuteBase, ImageReadCall):
    pass


class FileSearchExecute(CommandExecuteBase, FileSearchCall):
    pass


class FilePreviewExecute(CommandExecuteBase, FilePreviewCall):
    pass


class FileWriteExecute(CommandExecuteBase, FileWriteCall):
    pass


class CommandRunExecute(CommandExecuteBase, CommandRunCall):
    pass


class FileLinesExecute(CommandExecuteBase, FileLinesCall):
    pass


class FileGlobExecute(CommandExecuteBase, FileGlobCall):
    pass


class FileGrepExecute(CommandExecuteBase, FileGrepCall):
    pass


class FileEditPreviewExecute(CommandExecuteBase, FileEditPreviewCall):
    pass


class FileEditExecute(CommandExecuteBase, FileEditCall):
    pass


class ObserveExecute(CommandExecuteBase, ObserveCall):
    pass


class InteractExecute(CommandExecuteBase, InteractCall):
    pass


class PluginNodeExecute(CommandExecuteBase, PluginNodeCall):
    execution_timeout_seconds: StrictInt = Field(ge=1, le=60)

    @model_validator(mode="after")
    def check_mcp_timeout(self) -> Self:
        if self.execution_timeout_seconds > 10 and self.arguments.plugin_id != "builtin.mcp":
            raise ValueError("extended execution timeout is only available for MCP")
        return self


class AndroidDeviceStatusExecute(CommandExecuteBase, AndroidDeviceStatusCall):
    pass


class AndroidNotificationDigestExecute(CommandExecuteBase, AndroidNotificationDigestCall):
    pass


class FileReceiveExecute(CommandExecuteBase, FileReceiveCall):
    pass


CommandExecute = Annotated[
    SystemInfoExecute | ListDirectoryExecute | FileReadExecute | FileSearchExecute | FilePreviewExecute | FileWriteExecute | CommandRunExecute | FileLinesExecute | FileGlobExecute | FileGrepExecute | FileEditPreviewExecute | FileEditExecute | ObserveExecute | InteractExecute | ImageReadExecute | PluginNodeExecute | AndroidDeviceStatusExecute | AndroidNotificationDigestExecute | FileReceiveExecute, Field(discriminator="tool")
]


class CommandReceipt(SessionPayload):
    task_id: UUID
    attempt_id: UUID
    command_digest: CommandDigest
    boot_id: UUID


class CommandStatus(SessionPayload):
    attempt_id: UUID


class CommandResultBase(SessionPayload):
    task_id: UUID
    attempt_id: UUID
    command_digest: CommandDigest
    tool_version: Version1
    observed_at: UTCDateTime
    outcome: Literal["succeeded", "failed"]
    result: ToolResult | None = None
    error: ErrorPayload | None = None

    @model_validator(mode="after")
    def check_outcome(self) -> Self:
        if self.outcome == "succeeded":
            if self.result is None or self.error is not None:
                raise ValueError("succeeded requires result and no error")
        elif self.error is None or self.result is not None:
            raise ValueError("failed requires error and no result")
        return self


class SystemInfoCommandResult(CommandResultBase):
    tool: Literal["system.info"]
    result: SystemInfoResult | None = None


class ListDirectoryCommandResult(CommandResultBase):
    tool: Literal["filesystem.list"]
    result: ListDirectoryResult | None = None


class FileReadCommandResult(CommandResultBase):
    tool: Literal["filesystem.read"]
    result: FileReadResult | None = None


class ImageReadCommandResult(CommandResultBase):
    tool: Literal["filesystem.read_image"]
    result: ImageReadResult | None = None


class FileSearchCommandResult(CommandResultBase):
    tool: Literal["filesystem.search"]
    result: FileSearchResult | None = None


class FilePreviewCommandResult(CommandResultBase):
    tool: Literal["filesystem.write_preview"]
    result: FilePreviewResult | None = None


class FileWriteCommandResult(CommandResultBase):
    tool: Literal["filesystem.write"]
    result: FileWriteResult | None = None


class CommandRunCommandResult(CommandResultBase):
    tool: Literal["command.run"]
    result: CommandRunResult | None = None


class FileLinesCommandResult(CommandResultBase):
    tool: Literal["filesystem.read_lines"]
    result: FileLinesResult | None = None


class FileGlobCommandResult(CommandResultBase):
    tool: Literal["filesystem.glob"]
    result: FileGlobResult | None = None


class FileGrepCommandResult(CommandResultBase):
    tool: Literal["filesystem.grep"]
    result: FileGrepResult | None = None


class FileEditPreviewCommandResult(CommandResultBase):
    tool: Literal["filesystem.edit_preview"]
    result: FileEditPreviewResult | None = None


class FileEditCommandResult(CommandResultBase):
    tool: Literal["filesystem.edit"]
    result: FileEditResult | None = None


class ObserveCommandResult(CommandResultBase):
    tool: Literal["interaction.observe"]
    result: InteractionResult | None = None


class InteractCommandResult(CommandResultBase):
    tool: Literal["interaction.act"]
    result: InteractionResult | None = None


class PluginNodeCommandResult(CommandResultBase):
    tool: Literal["plugin.call"]
    result: PluginNodeResult | None = None


class AndroidDeviceStatusCommandResult(CommandResultBase):
    tool: Literal["android.device_status"]
    result: AndroidDeviceStatusResult | None = None


class AndroidNotificationDigestCommandResult(CommandResultBase):
    tool: Literal["android.notification_digest"]
    result: AndroidNotificationDigestResult | None = None

    @model_validator(mode="after")
    def notification_times(self) -> Self:
        from datetime import timedelta
        if self.result and any(entry.posted_at > self.observed_at + timedelta(seconds=5) for entry in self.result.entries):
            raise ValueError("notification timestamp is in the future")
        return self


class FileReceiveCommandResult(CommandResultBase):
    tool: Literal["filesystem.receive"]
    result: FileReceiveResult | None = None


CommandResult = Annotated[
    SystemInfoCommandResult | ListDirectoryCommandResult | FileReadCommandResult | FileSearchCommandResult | FilePreviewCommandResult | FileWriteCommandResult | CommandRunCommandResult | FileLinesCommandResult | FileGlobCommandResult | FileGrepCommandResult | FileEditPreviewCommandResult | FileEditCommandResult | ObserveCommandResult | InteractCommandResult | ImageReadCommandResult | PluginNodeCommandResult | AndroidDeviceStatusCommandResult | AndroidNotificationDigestCommandResult | FileReceiveCommandResult, Field(discriminator="tool")
]


class CommandStatusResult(SessionPayload):
    attempt_id: UUID
    phase: Literal["not_found", "received", "started", "completed", "cancelled", "unknown"]
    command_digest: CommandDigest | None = None
    result: CommandResult | None = None
    execution_stopped: StrictBool = False

    @model_validator(mode="after")
    def check_receipt(self) -> Self:
        if self.phase == "not_found":
            if self.command_digest is not None or self.result is not None or self.execution_stopped:
                raise ValueError("not_found cannot contain command evidence")
        elif self.command_digest is None:
            raise ValueError("known receipt requires command digest")
        if (self.phase == "completed") != (self.result is not None):
            raise ValueError("only completed receipt must contain result")
        if self.result is not None and (
            self.result.attempt_id != self.attempt_id
            or self.result.command_digest != self.command_digest
            or self.result.session_id != self.session_id
        ):
            raise ValueError("receipt result identity mismatch")
        return self


class CommandCancel(SessionPayload):
    attempt_id: UUID
    reason: StrictStr = Field(min_length=1, max_length=500)


class CommandCancelled(SessionPayload):
    attempt_id: UUID
    stopped_phase: Literal["received", "started"]


class ResultAck(SessionPayload):
    attempt_id: UUID
    status: Literal["stored", "already_settled"]


class WireError(ErrorPayload):
    session_id: UUID | None = None
