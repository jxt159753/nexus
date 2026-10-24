"""Binary delivery is a fixed, create-only workspace write, never executable data."""

from typing import Literal
from uuid import UUID

from pydantic import Field, StrictInt, StrictStr, model_validator

from nexus.contracts.attachments import MAX_UPLOAD_BYTES
from nexus.contracts.base import CommandDigest, StrictContract, Version1
from nexus.contracts.basic import relative_path


def receiving_path(request_id: UUID, filename: str) -> str:
    name = ''.join('_' if ord(c) < 32 or ord(c) == 127 or c in '/\\:*?<>|"' else c for c in filename).rstrip('. ')
    while len(name.encode('utf-8')) > 120:
        name = name[:-1]
    name = name.rstrip('. ') or 'file'
    try:
        relative_path(name)
    except ValueError:
        name = '_' + name
    return f'NEXUS-Inbox/{request_id}/{name}'


class FileReceiveArguments(StrictContract):
    root_id: Literal['workspace']
    conversation_id: UUID
    attachment_id: UUID
    path: StrictStr = Field(min_length=1, max_length=240)
    sha256: CommandDigest
    size_bytes: StrictInt = Field(ge=0, le=MAX_UPLOAD_BYTES)

    @model_validator(mode='after')
    def safe(self):
        relative_path(self.path)
        parts = self.path.split('/')
        if len(parts) != 3 or parts[0] != 'NEXUS-Inbox' or str(UUID(parts[1])) != parts[1]:
            raise ValueError('fixed receiving directory required')
        return self


class FileReceiveCall(StrictContract):
    tool: Literal['filesystem.receive']
    tool_version: Version1
    arguments: FileReceiveArguments


class FileReceiveResult(StrictContract):
    root_id: Literal['workspace']
    attachment_id: UUID
    path: StrictStr = Field(min_length=1, max_length=240)
    sha256: CommandDigest
    bytes_written: StrictInt = Field(ge=0, le=MAX_UPLOAD_BYTES)


class SendFileArguments(StrictContract):
    source_kind: Literal['attachment', 'deliverable']
    source_id: UUID


class ChatFile(StrictContract):
    source_kind: Literal['attachment', 'deliverable']
    source_id: UUID
    conversation_id: UUID
    filename: StrictStr
    size_bytes: StrictInt = Field(ge=0, le=MAX_UPLOAD_BYTES)
    sha256: CommandDigest
    download_url: StrictStr
    preview_url: StrictStr
    status: StrictStr = '可打开'
