"""Read-only conversation discovery and client navigation requests."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator
from nexus.contracts.base import StrictContract


class ConversationRouteArguments(StrictContract):
    action: Literal['search', 'read', 'open', 'project'] = 'search'
    query: StrictStr = Field(default='', max_length=120)
    project: StrictStr = Field(default='', max_length=100)
    conversation_id: UUID | None = None
    period: Literal['all', 'today', 'yesterday'] = 'all'
    date_from: StrictStr = Field(default='', max_length=10)
    date_until: StrictStr = Field(default='', max_length=10)
    include_archived: StrictBool = False
    before: StrictInt | None = Field(default=None, ge=1)
    limit: StrictInt = Field(default=6, ge=1, le=10)

    @model_validator(mode='after')
    def valid_selection(self):
        for value in (self.date_from, self.date_until):
            if value and date.fromisoformat(value).isoformat() != value:
                raise ValueError('dates must be YYYY-MM-DD')
        if self.date_from and self.date_until and self.date_from >= self.date_until:
            raise ValueError('date_until must be after date_from (exclusive)')
        if self.period != 'all' and (self.date_from or self.date_until):
            raise ValueError('use period or explicit dates')
        if self.action in {'read', 'open'} and self.conversation_id is None:
            raise ValueError('read/open requires a discovered conversation_id')
        if self.action == 'project' and not self.project.strip():
            raise ValueError('project requires a project name or ID')
        return self


class ConversationRouteProposal(StrictContract):
    intent: Literal['conversation_route']
    arguments: ConversationRouteArguments


class ConversationLink(StrictContract):
    conversation_id: UUID
    title: StrictStr
    project_name: StrictStr = ''
    turn_id: UUID | None = None
    created_at: StrictStr = ''
    archived: StrictBool = False


class ConversationNavigation(StrictContract):
    step_id: UUID
    conversation_id: UUID
