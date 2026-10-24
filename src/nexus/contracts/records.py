"""Bounded owner records and explicit standing consent for automatic memory."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from nexus.contracts.base import StrictContract, UTCDateTime


class ProjectCreate(StrictContract):
    name: StrictStr = Field(min_length=1, max_length=100)
    description: StrictStr = Field(default="", max_length=500)

    @model_validator(mode="after")
    def check_name(self) -> Self:
        if not self.name.strip():
            raise ValueError("nonempty project name required")
        return self


class MemoryCreate(StrictContract):
    request_id: UUID
    consent: StrictBool
    content: StrictStr = Field(min_length=1, max_length=2000)
    kind: Literal["fact", "inference", "plan"]
    verification: Literal["unverified", "verified"] = "unverified"
    source: StrictStr = Field(min_length=1, max_length=256)
    evidence_ref: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    verified_at: UTCDateTime | None = None
    valid_until: UTCDateTime | None = None
    project_id: UUID | None = None
    supersedes_id: UUID | None = None

    @model_validator(mode="after")
    def check_evidence(self) -> Self:
        if self.consent is not True:
            raise ValueError("explicit boolean consent required")
        if self.verification == "verified":
            if self.evidence_ref is None or not self.evidence_ref.strip() or self.verified_at is None:
                raise ValueError("verified requires evidence and verification time")
        elif self.verified_at is not None:
            raise ValueError("unverified cannot have verification time")
        if not self.content.strip() or not self.source.strip():
            raise ValueError("nonempty content and source required")
        return self


class MemorySelection(StrictContract):
    memory_ids: list[UUID] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len(set(self.memory_ids)) != len(self.memory_ids):
            raise ValueError("duplicate memories")
        return self


class MemoryPolicyUpdate(StrictContract):
    expected_revision: StrictInt = Field(ge=0)
    enabled: StrictBool
    consent_save: StrictBool = False
    consent_model: StrictBool = False

    @model_validator(mode="after")
    def consent(self) -> Self:
        if self.enabled and not (self.consent_save and self.consent_model):
            raise ValueError("saving and model reuse both require standing consent")
        return self


class MemoryCandidate(StrictContract):
    quote: StrictStr = Field(min_length=4, max_length=300)
    category: Literal["preference", "background", "ongoing"]
    scope: Literal["personal", "project"]

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if self.quote != self.quote.strip():
            raise ValueError("quote must be a trimmed exact excerpt")
        return self


class TodoCreate(StrictContract):
    request_id: UUID
    text: StrictStr = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.text.strip():
            raise ValueError("nonempty todo required")
        return self


class TodoUpdate(TodoCreate):
    expected_revision: StrictInt = Field(ge=1)
    status: Literal["planned", "done", "cancelled"]
    evidence_task_id: UUID | None = None

    @model_validator(mode="after")
    def evidence(self) -> Self:
        if (self.status == "done") != (self.evidence_task_id is not None):
            raise ValueError("only completed items require task evidence")
        return self
