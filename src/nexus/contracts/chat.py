"""Owner-authored chat input; roles and context are constructed by Core."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, TypeAdapter, model_validator

from nexus.contracts.base import StrictContract
from nexus.contracts.language import SystemProposal, DirectoryProposal

CHAT_PROPOSAL = TypeAdapter(Annotated[SystemProposal | DirectoryProposal, Field(discriminator="intent")])


class ChatActionDecision(StrictContract):
    proposal_digest: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["confirm", "reject"]


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
