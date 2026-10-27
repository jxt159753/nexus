"""Owner-selected expression preferences, separate from execution authority."""

from typing import Literal

from pydantic import Field, StrictInt

from nexus.contracts.base import StrictContract


class AssistantStyle(StrictContract):
    personality: Literal['nexus', 'companion', 'explorer', 'advisor'] = 'nexus'
    emoji: Literal['off', 'natural', 'expressive'] = 'natural'


class AssistantStyleUpdate(AssistantStyle):
    expected_revision: StrictInt = Field(ge=0)
