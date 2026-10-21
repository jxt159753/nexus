"""Owner-authored chat input; roles and context are constructed by Core."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, TypeAdapter, model_validator

from nexus.contracts.base import StrictContract
from nexus.contracts.language import SystemProposal, DirectoryProposal
from nexus.contracts.extensions import check_json_value, canonical


class SkillArguments(StrictContract):
    version_id: UUID
    input: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self):
        check_json_value(self.input)
        if len(canonical(self.input).encode()) > 8192:
            raise ValueError("chat Skill input exceeds 8 KiB")
        return self


class SkillProposal(StrictContract):
    intent: Literal["run_skill"]
    arguments: SkillArguments


CHAT_PROPOSAL = TypeAdapter(Annotated[SystemProposal | DirectoryProposal | SkillProposal, Field(discriminator="intent")])


class ChatPlanPreview(StrictContract):
    input: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self):
        check_json_value(self.input)
        if len(canonical(self.input).encode()) > 8192:
            raise ValueError("chat Skill input exceeds 8 KiB")
        return self

class ChatResultPreview(StrictContract):
    fields: list[Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]] | None = Field(default=None, min_length=1, max_length=7)


class ChatResultConsent(StrictContract):
    result_digest: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: StrictInt = Field(ge=1)
    allow_model: StrictBool = False


class ChatActionDecision(StrictContract):
    proposal_digest: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["confirm", "reject"]
    allow_result_model: StrictBool = False

    @model_validator(mode="after")
    def result_requires_action(self):
        if self.allow_result_model and self.decision != "confirm":
            raise ValueError("result consent requires action confirmation")
        return self


class ConversationCreate(StrictContract):
    request_id: UUID
    project_id: UUID | None = None


class ConversationUpdate(StrictContract):
    title: StrictStr | None = Field(default=None, min_length=1, max_length=80)
    archived: StrictBool | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if (self.title is None and self.archived is None) or (self.title is not None and not self.title.strip()):
            raise ValueError("nonempty update required")
        return self


class ChatSubmission(StrictContract):
    request_id: UUID
    expected_revision: StrictInt = Field(ge=1)
    text: StrictStr = Field(min_length=1, max_length=2000)
    memory_ids: list[UUID] = Field(default_factory=list, max_length=8)
    memory_digest: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    allow_memory_model: StrictBool = False
    include_context: StrictBool = True
    allow_compaction: StrictBool = False
    compact_only: StrictBool = False
    allow_model: StrictBool = False
    allow_tools: StrictBool = False
    allow_skills: StrictBool = False
    skills_digest: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    node_id: UUID | None = None
    work_grant_id: UUID | None = None
    work_digest: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    allow_work_results: StrictBool = False
    workspace_work: StrictBool = False
    enqueue: StrictBool = False
    reference_turn_ids: list[UUID] = Field(default_factory=list, max_length=3)
    references_digest: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    allow_reference_model: StrictBool = False

    @model_validator(mode="after")
    def bounded(self):
        if self.compact_only and (not self.allow_compaction or not self.include_context or self.text.strip() != "/compact"
                                 or self.allow_tools or self.memory_ids or self.reference_turn_ids):
            raise ValueError("/compact requires context and compaction consent without tools or selected private references")
        if len(set(self.reference_turn_ids)) != len(self.reference_turn_ids):
            raise ValueError("duplicate references")
        if bool(self.reference_turn_ids) != bool(self.references_digest) or bool(self.reference_turn_ids) != self.allow_reference_model:
            raise ValueError("references require preview and separate outbound consent")
        if self.work_grant_id and self.workspace_work:
            raise ValueError("select one work scope")
        if (self.work_grant_id or self.workspace_work) and (not self.allow_tools or not self.allow_work_results or not self.work_digest):
            raise ValueError("work requires a grant, catalog preview and outbound result consent")
        if not (self.work_grant_id or self.workspace_work) and (self.work_digest or self.allow_work_results):
            raise ValueError("work consent requires a grant")
        if len(set(self.memory_ids)) != len(self.memory_ids):
            raise ValueError("duplicate memory selection")
        if self.memory_ids and (not self.allow_memory_model or self.memory_digest is None):
            raise ValueError("memory requires preview and separate outbound consent")
        if not self.memory_ids and (self.memory_digest is not None or self.allow_memory_model):
            raise ValueError("memory consent must select records")
        if not self.text.strip() or len(self.text.encode("utf-8")) > 6000:
            raise ValueError("bounded nonempty text required")
        if self.allow_tools and self.node_id is None:
            raise ValueError("tool suggestions require an explicit node")
        if self.allow_skills and (not self.allow_tools or self.skills_digest is None):
            raise ValueError("Skill suggestions require tool consent and a previewed catalog")
        return self


class ChatSteering(StrictContract):
    request_id: UUID
    text: StrictStr = Field(min_length=1, max_length=2000)
    allow_model: StrictBool

    @model_validator(mode="after")
    def confirmed(self):
        if not self.allow_model or not self.text.strip() or len(self.text.encode()) > 6000:
            raise ValueError("bounded correction and explicit outbound consent required")
        return self


class ChatResume(StrictContract):
    request_id: UUID
    consent: StrictBool

    @model_validator(mode="after")
    def confirmed(self):
        if not self.consent:
            raise ValueError("explicit resume required")
        return self


class ChatReferences(StrictContract):
    turn_ids: list[UUID] = Field(min_length=1, max_length=3)
