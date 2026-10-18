"""Explicit, bounded owner input; memory is never extracted automatically."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictStr, model_validator

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
