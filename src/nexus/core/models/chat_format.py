"""Bounded chat metadata and the two existing function schemas."""

import json
from collections.abc import Awaitable, Callable

from pydantic import Field, StrictStr, ValidationError, model_validator

from nexus.contracts.base import StrictContract
from nexus.contracts.chat import CHAT_PROPOSAL
from nexus.contracts.extensions import parse_strict_json
from nexus.contracts.task import ListDirectoryArguments, SystemInfoArguments
from nexus.contracts.records import MemoryCandidate


class ChatTitle(StrictContract):
    title: StrictStr = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def single_line(self):
        if not self.title.strip() or any(ord(c)<32 for c in self.title):
            raise ValueError("single line title required")
        return self


class ChatMetadata(StrictContract):
    title: StrictStr | None = Field(default=None, min_length=1, max_length=40)
    memories: list[MemoryCandidate] = Field(max_length=3)


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
    """Stream prose while keeping bounded reserved metadata out of public output."""
    def __init__(self, generate_title: bool, receive: Callable[[str], Awaitable[None]], *, auto_memory=False, allow_progress=False):
        self.pending = self.title_requested = generate_title
        self.auto_memory = auto_memory
        self.metadata_buffer = ""
        self.in_metadata = self.discard_metadata = self.after_metadata = self.metadata_seen = False
        self.discard_response = False
        self.memories = ()
        self.buffer = ""
        self.title = None
        self.parts = []
        self.receive = receive
        self.progress_pending = allow_progress
        self.progress_buffer = ""
        self.public_progress = False

    @staticmethod
    def _object_end(text):
        depth, quoted, escaped = 0, False, False
        for index, char in enumerate(text):
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
                if depth == 0:
                    return index + 1
        return None

    async def feed(self, value: str, *, final: bool = False):
        if self.discard_response:
            return
        if not self.auto_memory:
            await self._text(value, final=final)
            return
        self.metadata_buffer += value
        prefix = "NEXUS_META"
        while self.metadata_buffer:
            if self.after_metadata:
                self.metadata_buffer = self.metadata_buffer.lstrip()
                if not self.metadata_buffer:
                    break
                self.after_metadata = False
            if self.in_metadata:
                self.metadata_buffer = self.metadata_buffer.lstrip() if not self.discard_metadata else self.metadata_buffer
                probe = self.metadata_buffer
                if not probe:
                    break
                end = self._object_end(probe) if probe.startswith("{") and not self.discard_metadata else None
                if end is not None and len(probe[:end].encode()) <= 4096:
                    try:
                        metadata = ChatMetadata.model_validate(parse_strict_json(probe[:end]))
                        if not self.metadata_seen:
                            if self.title_requested and metadata.title:
                                self.title = ChatTitle(title=metadata.title).title.strip()
                            self.memories = tuple(item.model_dump() for item in metadata.memories)
                            self.metadata_seen = True
                    except (ValidationError, ValueError):
                        pass
                else:
                    # Invalid or oversized reserved lines are discarded, never displayed.
                    if not self.discard_metadata and probe.startswith("{"):
                        if len(probe.encode()) <= 4096 and not final:
                            break
                        # An unterminated/oversized JSON object has no safe prose boundary.
                        self.metadata_buffer = ""
                        self.discard_response = True
                        return
                    newline = probe.find("\n")
                    if newline < 0:
                        if len(probe.encode()) > 4096:
                            self.metadata_buffer = ""
                            self.discard_metadata = True
                        break
                    end = newline + 1
                self.metadata_buffer = probe[end:]
                self.in_metadata = self.discard_metadata = self.pending = False
                self.after_metadata = True
                continue
            position = self.metadata_buffer.find(prefix)
            if position >= 0:
                prose = self.metadata_buffer[:position]
                if prose.strip() or self.parts:
                    await self._text(prose)
                self.metadata_buffer = self.metadata_buffer[position+len(prefix):]
                self.in_metadata = True
                continue
            # Hold only a possible split marker, so ordinary prose streams immediately.
            keep = 0
            if not final:
                for size in range(1, min(len(prefix), len(self.metadata_buffer)+1)):
                    if self.metadata_buffer.endswith(prefix[:size]):
                        keep = size
            prose = self.metadata_buffer[:-keep] if keep else self.metadata_buffer
            self.metadata_buffer = self.metadata_buffer[-keep:] if keep else ""
            if prose.strip() or self.parts:
                await self._text(prose)
            break
        if final:
            self.metadata_buffer = ""
            await self._text("", final=True)

    async def _text(self, value: str, *, final: bool = False):
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
        if self.progress_pending:
            self.progress_buffer += value
            probe = self.progress_buffer.lstrip()
            marker = "NEXUS_PROGRESS:"
            if marker.startswith(probe) and not final:
                return
            self.public_progress = probe.startswith(marker)
            value = probe[len(marker):].lstrip() if self.public_progress else self.progress_buffer
            self.progress_pending = False
            self.progress_buffer = ""
        if value:
            self.parts.append(value)
            await self.receive(value)
