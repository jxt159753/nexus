"""Explicit conversation grants and native, bounded work calls."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, TypeAdapter, model_validator

from nexus.contracts.base import StrictContract
from nexus.contracts.basic import FileReadArguments, FileSearchArguments, FilePreviewArguments, CommandId
from nexus.contracts.chat import SkillArguments, SkillProposal
from nexus.contracts.language import SystemProposal, DirectoryProposal


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


class ToolProposal(StrictContract):
    intent: Literal["run_tool"]
    arguments: SkillArguments


WORK_PROPOSAL = TypeAdapter(Annotated[SystemProposal | DirectoryProposal | ReadProposal | SearchProposal | WriteProposal | CommandProposal | StatusProposal | SkillProposal | ToolProposal, Field(discriminator="intent")])
