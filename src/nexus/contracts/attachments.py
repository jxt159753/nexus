"""Conversation uploads are data, never executable paths or model permissions."""

from uuid import UUID

from pydantic import Field, StrictStr, model_validator

from nexus.contracts.base import StrictContract

MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_UPLOAD_BASE64 = 4 * ((MAX_UPLOAD_BYTES + 2) // 3)


class UploadMetadata(StrictContract):
    request_id: UUID
    filename: StrictStr = Field(min_length=1, max_length=200)
    media_type: StrictStr = Field(default="application/octet-stream", max_length=128,
        pattern=r"^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$")

    @model_validator(mode="after")
    def display_name(self):
        if (not self.filename.strip() or self.filename in {".", ".."} or len(self.filename.encode()) > 600
                or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in self.filename)):
            raise ValueError("a bounded display filename, not a path, is required")
        return self


class UploadJSON(UploadMetadata):
    data_base64: StrictStr = Field(max_length=MAX_UPLOAD_BASE64)


class AttachmentSelection(StrictContract):
    attachment_ids: list[UUID] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("duplicate attachment selection")
        return self
