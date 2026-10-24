"""Explicit conversation grants and native, bounded work calls."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, TypeAdapter, model_validator

from nexus.contracts.interaction import WorkObserveArguments, WorkInteractArguments
from nexus.contracts.images import ImageReadArguments
from nexus.contracts.base import StrictContract
from nexus.contracts.basic import FileLinesArguments, FileGlobArguments, FileGrepArguments, FileEditPreviewArguments, FileReadArguments, FileSearchArguments, FilePreviewArguments, CommandId
from nexus.contracts.chat import SkillArguments, SkillProposal
from nexus.contracts.language import SystemProposal, DirectoryProposal
from nexus.contracts.web import WebSearchArguments, WebFetchArguments
from nexus.plugins.contracts import PluginInspectProposal, PluginExecuteProposal, PluginInstallProposal
from nexus.contracts.extensions import SkillGeneration


class WorkGrantCreate(StrictContract):
    request_id: UUID
    node_id: UUID
    consent: StrictBool
    profile: Literal["read", "standard"] = "standard"
    duration_minutes: StrictInt = Field(default=30, ge=1, le=30)
    max_operations: StrictInt = Field(default=20, ge=1, le=20)

    @model_validator(mode="after")
    def confirmed(self):
        if not self.consent:
            raise ValueError("explicit conversation permission required")
        return self


class WorkPermissionReply(StrictContract):
    request_id: UUID
    step_id: UUID
    expected_revision: StrictInt = Field(ge=1)
    text: StrictStr = Field(min_length=1, max_length=100)


class ReadProposal(StrictContract):
    intent: Literal["read_file"]
    arguments: FileReadArguments


class SearchProposal(StrictContract):
    intent: Literal["find_files"]
    arguments: FileSearchArguments


class WriteProposal(StrictContract):
    intent: Literal["write_file"]
    arguments: FilePreviewArguments


class WorkCommandArguments(StrictContract):
    root_id: Literal["workspace"]
    command_id: CommandId


class CommandProposal(StrictContract):
    intent: Literal["run_command"]
    arguments: WorkCommandArguments


class StatusProposal(StrictContract):
    intent: Literal["get_node_status"]
    arguments: StrictContract


class TimeArguments(StrictContract):
    utc_offset_minutes: StrictInt = Field(default=0, ge=-720, le=840)


class TimeProposal(StrictContract):
    intent: Literal["get_current_time"]
    arguments: TimeArguments


class JobListArguments(StrictContract):
    limit: StrictInt = Field(default=10, ge=1, le=20)


class JobListProposal(StrictContract):
    intent: Literal["job_list"]
    arguments: JobListArguments


class JobOutputArguments(StrictContract):
    task_id: UUID
    max_bytes: StrictInt = Field(default=4096, ge=512, le=8192)


class JobOutputProposal(StrictContract):
    intent: Literal["job_output"]
    arguments: JobOutputArguments


class ImageProposal(StrictContract):
    intent: Literal["read_image"]
    arguments: ImageReadArguments


class ToolProposal(StrictContract):
    intent: Literal["run_tool"]
    arguments: SkillArguments


class CapabilityArguments(StrictContract):
    version_id: UUID


class CapabilityProposal(StrictContract):
    intent: Literal["inspect_capability"]
    arguments: CapabilityArguments


class SkillCreateArguments(StrictContract):
    reason: StrictStr = Field(min_length=1, max_length=500)
    skill: SkillGeneration

    @model_validator(mode="after")
    def tested_plan(self):
        ids = [step.id for step in self.skill.steps]
        if not self.reason.strip() or not self.skill.tests or any(case.expected_steps != ids for case in self.skill.tests):
            raise ValueError("creation requires a reason and tests covering the ordered steps")
        return self


class SkillCreateProposal(StrictContract):
    intent: Literal["skill_create"]
    arguments: SkillCreateArguments


class WebSearchProposal(StrictContract):
    intent: Literal["web_search"]
    arguments: WebSearchArguments


class WebFetchProposal(StrictContract):
    intent: Literal["web_fetch"]
    arguments: WebFetchArguments


class LinesProposal(StrictContract):
    intent: Literal["read"]
    arguments: FileLinesArguments


class GlobProposal(StrictContract):
    intent: Literal["glob"]
    arguments: FileGlobArguments


class GrepProposal(StrictContract):
    intent: Literal["grep"]
    arguments: FileGrepArguments


class EditProposal(StrictContract):
    intent: Literal["edit"]
    arguments: FileEditPreviewArguments


class ObserveProposal(StrictContract):
    intent: Literal["observe_target"]
    arguments: WorkObserveArguments


class InteractProposal(StrictContract):
    intent: Literal["interact_target"]
    arguments: WorkInteractArguments


from nexus.contracts.android import AndroidDeviceStatusArguments, AndroidNotificationDigestArguments


class AndroidDeviceStatusProposal(StrictContract):
    intent: Literal["android_device_status"]
    arguments: AndroidDeviceStatusArguments


class AndroidNotificationDigestProposal(StrictContract):
    intent: Literal["android_notification_digest"]
    arguments: AndroidNotificationDigestArguments


from nexus.contracts.file_transfer import SendFileArguments


class SendFileProposal(StrictContract):
    intent: Literal['send_file']
    arguments: SendFileArguments


WORK_ACTION = Annotated[SendFileProposal | AndroidDeviceStatusProposal | AndroidNotificationDigestProposal | SystemProposal | DirectoryProposal | ReadProposal | SearchProposal | WriteProposal | CommandProposal | StatusProposal | SkillProposal | ToolProposal | CapabilityProposal | SkillCreateProposal | WebSearchProposal | WebFetchProposal | LinesProposal | GlobProposal | GrepProposal | EditProposal | ObserveProposal | InteractProposal | ImageProposal | TimeProposal | JobListProposal | JobOutputProposal | PluginInspectProposal | PluginExecuteProposal | PluginInstallProposal, Field(discriminator="intent")]


class NodeAction(StrictContract):
    node_id: UUID
    action: WORK_ACTION


class RoutedProposal(StrictContract):
    intent: Literal["on_node"]
    arguments: NodeAction


WORK_PROPOSAL = TypeAdapter(Annotated[WORK_ACTION | RoutedProposal, Field(discriminator="intent")])
