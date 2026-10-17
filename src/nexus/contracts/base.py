"""Shared validation primitives; no execution or application state."""

import re
from datetime import datetime, timedelta
from typing import Annotated

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
)

MAX_MESSAGE_BYTES = 64 * 1024
MAX_RESULT_BYTES = 32 * 1024
Version1 = Annotated[StrictInt, Field(ge=1, le=1)]
EventId = Annotated[StrictStr, Field(pattern=r"^(0|[1-9][0-9]{0,18})$")]
Revision = Annotated[StrictInt, Field(ge=0)]
CommandDigest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _timestamp_text(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)",
        value,
    ):
        raise ValueError("timestamp must be a UTC RFC 3339 string")
    return datetime.fromisoformat(value)


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return value


UTCDateTime = Annotated[
    AwareDatetime, BeforeValidator(_timestamp_text), AfterValidator(_require_utc)
]
