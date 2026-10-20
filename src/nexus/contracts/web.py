"""Public web requests; credentials and transport settings are Core-owned."""

import ipaddress
from urllib.parse import urlsplit

from pydantic import Field, StrictInt, StrictStr, field_validator

from nexus.contracts.base import StrictContract


def public_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").rstrip(".").lower()
    if (len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value)
            or "\\" in value or parsed.scheme not in {"http", "https"}
            or not host or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None, 80, 443}):
        raise ValueError("public HTTP(S) URL required")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".test", ".invalid")):
            raise ValueError("public host required") from None
    else:
        if not address.is_global:
            raise ValueError("public address required")
    return value


class WebSearchArguments(StrictContract):
    query: StrictStr = Field(min_length=1, max_length=128)
    count: StrictInt = Field(default=5, ge=1, le=5)

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("nonblank query required")
        return value


class WebFetchArguments(StrictContract):
    url: StrictStr = Field(min_length=1, max_length=2048)
    _public = field_validator("url")(public_url)
