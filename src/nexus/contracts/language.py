"""Finite natural-language submissions and untrusted action proposals."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictStr, model_validator

from nexus.contracts.base import StrictContract
from nexus.contracts.task import ListDirectoryArguments, SystemInfoArguments


class LanguageSubmission(StrictContract):
    request_id: UUID
    node_id: UUID
    project_id: UUID | None = None
    text: StrictStr = Field(min_length=1, max_length=2000)
    allow_model: StrictBool = False

    @model_validator(mode="after")
    def bounded_text(self) -> Self:
        if not self.text.strip() or len(self.text.encode("utf-8")) > 6000:
            raise ValueError("nonempty bounded text required")
        return self


class SystemProposal(StrictContract):
    intent: Literal["get_system_info"]
    arguments: SystemInfoArguments


class DirectoryProposal(StrictContract):
    intent: Literal["list_directory"]
    arguments: ListDirectoryArguments


class UnsupportedProposal(StrictContract):
    intent: Literal["unsupported"]
    arguments: SystemInfoArguments
