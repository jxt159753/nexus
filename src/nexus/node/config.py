"""Explicit local policy. Loading configuration performs no execution."""

import os
import stat
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictInt, StrictStr, model_validator
from nexus.contracts.basic import ToolKey, CommandId
from nexus.contracts.base import CommandDigest, MAX_RESULT_BYTES, StrictContract
from nexus.plugins.config import PluginNodeConfig


class CommandTemplate(StrictContract):
    executable: Path
    executable_sha256: CommandDigest
    argv: list[StrictStr] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def safe(self):
        if not self.executable.is_absolute() or not self.executable.is_file():
            raise ValueError("existing absolute executable required")
        local_directory(self.executable.parent)
        if self.executable.suffix.lower() in {".bat", ".cmd", ".ps1", ".sh"} or self.executable.stem.lower() in {"cmd", "powershell", "pwsh", "bash", "sh", "wscript", "cscript"}:
            raise ValueError("shell templates are not allowed")
        if sum(len(arg.encode()) for arg in self.argv)>2048 or any("\x00" in arg for arg in self.argv):
            raise ValueError("bounded literal arguments required")
        return self

    def digest(self):
        import hashlib
        import json
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def local_directory(path: Path) -> Path:
    # Do not resolve() before checking: it would conceal junctions and symlinks.
    text = str(path)
    if (not path.is_absolute() or text.startswith(("\\\\", "//"))
            or any(c in text for c in "*?\x00") or ".." in path.parts
            or ":" in text[2:]):
        raise ValueError("absolute local directory required")
    if os.name == "nt":
        from nexus.node.windows import require_local_drive
        require_local_drive(path.anchor)
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or not stat.S_ISDIR(info.st_mode)):
            raise ValueError("directory and ancestors must not be reparse points")
    return path


class NodeConfig(StrictContract):
    node_id: UUID
    data_dir: Path
    allowed_tools: frozenset[ToolKey] = frozenset()
    interaction_targets: dict[CommandId, "InteractionTarget"] = Field(default_factory=dict, max_length=16)
    commands: dict[CommandId, CommandTemplate] = Field(default_factory=dict, max_length=16)
    plugins: PluginNodeConfig = Field(default_factory=PluginNodeConfig)
    roots: dict[Literal["workspace"], Path] = Field(default_factory=dict, max_length=1)
    max_result_bytes: StrictInt = Field(default=MAX_RESULT_BYTES, ge=128, le=MAX_RESULT_BYTES)
    execution_timeout_seconds: StrictInt = Field(default=10, ge=1, le=10)
    max_receipts: StrictInt = Field(default=10000, ge=1, le=10000)

    def plugin_digest(self):
        from nexus.contracts.extensions import digest_value
        policy=self.plugins.digest()
        if policy is None: return None
        return digest_value({'runtime':policy,'roots':{key:str(value) for key,value in self.roots.items()},
            'interaction_targets':{key:value.digest() for key,value in self.interaction_targets.items()}})

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        local_directory(self.data_dir)
        for path in self.roots.values():
            local_directory(path)
        if len(self.model_dump_json().encode("utf-8"))>24576:
            raise ValueError("local tool policy exceeds 24 KiB worker budget")
        return self


from nexus.node.tools.interaction_config import InteractionTarget
NodeConfig.model_rebuild()
