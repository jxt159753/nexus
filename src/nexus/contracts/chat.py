"""Owner-authored chat input; roles and context are constructed by Core."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, TypeAdapter, model_validator

from nexus.contracts.base import StrictContract
from nexus.contracts.language import SystemProposal, DirectoryProposal

CHAT_PROPOSAL = TypeAdapter(Annotated[SystemProposal | DirectoryProposal, Field(discriminator="intent")])

ResultField = Literal["os_name", "os_version", "architecture", "python_version", "root_id", "entries", "truncated"]


class ChatResultPreview(StrictContract):
    fields: list[ResultField] | None = Field(default=None, min_length=1, max_length=7)


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
    include_context: StrictBool = True
    allow_model: StrictBool = False
    allow_tools: StrictBool = False
    node_id: UUID | None = None

    @model_validator(mode="after")
    def bounded(self):
        if not self.text.strip() or len(self.text.encode("utf-8")) > 6000:
            raise ValueError("bounded nonempty text required")
        if self.allow_tools and self.node_id is None:
            raise ValueError("tool suggestions require an explicit node")
        return self
