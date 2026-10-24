"""Local administrator-authored browser/app recipes, pinned into Core policy."""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, StrictStr, model_validator

from nexus.contracts.base import StrictContract, CommandDigest
from nexus.contracts.basic import CommandId


def origin(url):
    parsed=urlsplit(url)
    if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("HTTP(S) origin without credentials required")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


class BrowserAction(StrictContract):
    kind: Literal["navigate","click","fill"]
    description: StrictStr = Field(min_length=1,max_length=200)
    selector: StrictStr | None = Field(default=None,min_length=1,max_length=300)
    url: StrictStr | None = Field(default=None,max_length=2048)

    @model_validator(mode="after")
    def shape(self):
        if self.kind=="navigate":
            if self.selector is not None or self.url is None:
                raise ValueError("fixed URL required")
        elif self.selector is None or self.url is not None:
            raise ValueError("fixed selector required")
        return self


class Target(StrictContract):
    def digest(self):
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"),sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()


class BrowserTarget(Target):
    kind: Literal["edge","chrome"]
    endpoint: StrictStr = Field(max_length=100)
    allowed_origins: list[StrictStr] = Field(min_length=1,max_length=8)
    actions: dict[CommandId,BrowserAction] = Field(default_factory=dict,max_length=16)

    @model_validator(mode="after")
    def local(self):
        parsed=urlsplit(self.endpoint)
        if parsed.scheme!="http" or parsed.hostname!="127.0.0.1" or not parsed.port or parsed.path not in {"","/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("dedicated loopback CDP endpoint required")
        if any(origin(value)!=value for value in self.allowed_origins):
            raise ValueError("exact normalized origins required")
        for action in self.actions.values():
            if action.url and origin(action.url) not in self.allowed_origins:
                raise ValueError("navigation outside configured origins")
        return self


class AppAction(StrictContract):
    kind: Literal["focus","invoke","set_value"]
    description: StrictStr = Field(min_length=1,max_length=200)
    automation_id: StrictStr = Field(min_length=1,max_length=200)


class AppTarget(Target):
    kind: Literal["windows_app"]
    executable: Path
    executable_sha256: CommandDigest
    actions: dict[CommandId,AppAction] = Field(min_length=1,max_length=16)

    @model_validator(mode="after")
    def local(self):
        from nexus.node.config import local_directory
        if not self.executable.is_absolute() or self.executable.suffix.lower()!=".exe" or not self.executable.is_file():
            raise ValueError("existing configured .exe required")
        local_directory(self.executable.parent)
        if self.executable.stem.lower() in {"cmd","powershell","pwsh","wscript","cscript","mshta"}:
            raise ValueError("shell applications are not supported")
        return self


InteractionTarget=Annotated[BrowserTarget | AppTarget,Field(discriminator="kind")]
