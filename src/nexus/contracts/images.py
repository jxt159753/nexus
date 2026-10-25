"""Small verified raster snapshots; Core's main process imports no decoder."""

from nexus.contracts.host_access import HostAccess
import base64
import hashlib
import struct
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from nexus.contracts.base import CommandDigest, MAX_RESULT_BYTES, StrictContract
from nexus.contracts.basic import relative_path

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 8192
MAX_IMAGE_SIDE = 1024
SOURCE_MEDIA = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}


class ImageReadArguments(HostAccess):
    root_id: Literal["workspace"]
    path: StrictStr = Field(min_length=1, max_length=240)
    max_dimension: StrictInt = Field(default=1024, ge=128, le=MAX_IMAGE_SIDE)

    @model_validator(mode="after")
    def safe_path(self):
        relative_path(self.path)
        return self


def raster_dimensions(raw: bytes, media_type: str) -> tuple[int, int]:
    """Check normalized PNG/JPEG headers, not a second full decoder."""
    if media_type == "image/png":
        if (len(raw) < 45 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[8:16] != b"\x00\x00\x00\rIHDR"
                or raw[-12:] != b"\x00\x00\x00\x00IEND\xaeB`\x82"):
            raise ValueError("invalid normalized PNG")
        return struct.unpack(">II", raw[16:24])
    if media_type != "image/jpeg" or not raw.startswith(b"\xff\xd8") or not raw.endswith(b"\xff\xd9"):
        raise ValueError("invalid normalized JPEG")
    offset = 2
    while offset + 4 <= len(raw):
        if raw[offset] != 255:
            break
        while offset < len(raw) and raw[offset] == 255:
            offset += 1
        if offset >= len(raw):
            break
        marker = raw[offset]
        offset += 1
        length = int.from_bytes(raw[offset:offset+2], "big")
        if length < 2 or offset + length > len(raw):
            break
        if marker in {0xC0, 0xC2} and length >= 8:
            height, width = struct.unpack(">HH", raw[offset+3:offset+7])
            return width, height
        if marker == 0xDA:
            break
        offset += length
    raise ValueError("normalized JPEG dimensions missing")


class RasterImage(StrictContract):
    media_type: Literal["image/png", "image/jpeg"]
    width: StrictInt = Field(ge=1, le=MAX_IMAGE_SIDE)
    height: StrictInt = Field(ge=1, le=MAX_IMAGE_SIDE)
    size_bytes: StrictInt = Field(ge=1, le=MAX_IMAGE_BYTES)
    sha256: CommandDigest
    data_base64: StrictStr = Field(min_length=4, max_length=10924)

    @model_validator(mode="after")
    def evidence(self):
        raw = base64.b64decode(self.data_base64, validate=True)
        if (base64.b64encode(raw).decode("ascii") != self.data_base64 or len(raw) != self.size_bytes
                or hashlib.sha256(raw).hexdigest() != self.sha256
                or raster_dimensions(raw, self.media_type) != (self.width, self.height)):
            raise ValueError("image evidence mismatch")
        return self


class ImageSnapshot(StrictContract):
    source_sha256: CommandDigest
    source_size_bytes: StrictInt = Field(ge=1, le=MAX_SOURCE_BYTES)
    source_media_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
    original_width: StrictInt = Field(ge=1, le=8192)
    original_height: StrictInt = Field(ge=1, le=8192)
    image: RasterImage
    downscaled: StrictBool
    first_frame_only: StrictBool

    @model_validator(mode="after")
    def bounded(self):
        if (self.original_width * self.original_height > 16_000_000
                or self.image.width > self.original_width or self.image.height > self.original_height
                or self.downscaled != ((self.image.width, self.image.height) != (self.original_width, self.original_height))
                or self.source_media_type in {"image/gif", "image/webp"} and not self.first_frame_only
                or self.source_media_type == "image/jpeg" and self.first_frame_only
                or len(self.model_dump_json().encode()) > MAX_RESULT_BYTES):
            raise ValueError("invalid bounded image snapshot")
        return self


class ImageReadResult(ImageSnapshot):
    root_id: Literal["workspace"]
    path: StrictStr

    @model_validator(mode="after")
    def scoped_path(self):
        relative_path(self.path)
        return self


def image_url(image: dict) -> str:
    checked = RasterImage.model_validate(image)
    return f"data:{checked.media_type};base64,{checked.data_base64}"


def validate_image_messages(messages: list[dict], enabled: bool) -> int:
    count = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if message.get("role") != "user" or not enabled:
            raise ValueError("image input is disabled or in an invalid role")
        for block in content:
            if not isinstance(block, dict):
                raise ValueError("invalid content block")
            if block.get("type") == "text" and set(block) == {"type", "text"} and isinstance(block["text"], str):
                continue
            if set(block) != {"type", "image_url"} or block["type"] != "image_url" or set(block["image_url"]) != {"url", "detail"} or block["image_url"]["detail"] != "original":
                raise ValueError("bounded inline image required")
            prefix, separator, encoded = block["image_url"]["url"].partition(";base64,")
            if not separator or prefix not in {"data:image/png", "data:image/jpeg"} or len(encoded) > 10924:
                raise ValueError("remote or oversized image refused")
            raw = base64.b64decode(encoded, validate=True)
            width, height = raster_dimensions(raw, prefix[5:])
            RasterImage(media_type=prefix[5:], width=width, height=height, size_bytes=len(raw),
                        sha256=hashlib.sha256(raw).hexdigest(), data_base64=encoded)
            count += 1
    if count > 4:
        raise ValueError("too many image snapshots")
    return count
