"""Bounded chat metadata and the two existing function schemas."""

import json
from collections.abc import Awaitable, Callable

from pydantic import Field, StrictStr, ValidationError, model_validator

from nexus.contracts.base import StrictContract
from nexus.contracts.chat import CHAT_PROPOSAL
from nexus.contracts.extensions import parse_strict_json
from nexus.contracts.task import ListDirectoryArguments, SystemInfoArguments


class ChatTitle(StrictContract):
    title: StrictStr = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def single_line(self):
        if not self.title.strip() or any(ord(c)<32 for c in self.title):
            raise ValueError("single line title required")
        return self


TOOLS = [
    {"type":"function", "function":{"name":name, "description":description, "parameters":arguments.model_json_schema()}}
    for name,description,arguments in [
        ("get_system_info", "Propose reading the selected device's OS, architecture and Python version. User confirmation is required; this call does not execute anything.", SystemInfoArguments),
        ("list_directory", "Propose listing direct entries in the configured workspace alias. No arbitrary paths or file contents. Requires confirmation and separate directory approval.", ListDirectoryArguments),
    ]
]


def parse_proposal(name: str, arguments: str) -> dict:
    return CHAT_PROPOSAL.validate_json(json.dumps({"intent":name,"arguments":parse_strict_json(arguments)}, allow_nan=False)).model_dump(mode="json")


def skill_tool(catalog: list[dict]) -> dict:
    return {"type": "function", "function": {
        "name": "run_skill",
        "description": "Propose one enabled, fixed Skill below. Never execute or enable a capability. "
                       "Ask for missing inputs or leave them for the user's plan form. Catalog: " + json.dumps(catalog, ensure_ascii=False),
        "parameters": {"type": "object", "properties": {
            "version_id": {"type": "string", "enum": [item["version_id"] for item in catalog]},
            "input": {"type": "object"}}, "required": ["version_id", "input"], "additionalProperties": False}}}


class ChatText:
    """Strip a valid first-line title while streaming the actual answer."""
    def __init__(self, generate_title: bool, receive: Callable[[str], Awaitable[None]]):
        self.pending=generate_title
        self.buffer=""
        self.title=None
        self.parts=[]
        self.receive=receive

    async def feed(self, value: str, *, final: bool = False):
        if self.pending:
            self.buffer+=value
            if "\n" not in self.buffer and len(self.buffer.encode())<=512 and (not self.buffer.strip() or self.buffer.lstrip().startswith("{")) and not final:
                return
            first,separator,rest=self.buffer.partition("\n")
            value=self.buffer
            try:
                self.title=ChatTitle.model_validate_json(first).title.strip()
                value=rest if separator else ""
            except (ValidationError, ValueError):
                pass
            self.pending=False
            self.buffer=""
        if value:
            self.parts.append(value)
            await self.receive(value)
