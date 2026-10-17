"""Explicit local policy. Loading configuration performs no execution."""

import os
import stat
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictInt, model_validator

from nexus.contracts.base import MAX_RESULT_BYTES, StrictContract

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
    allowed_tools: frozenset[Literal["system.info@1", "filesystem.list@1"]] = frozenset()
    roots: dict[Literal["workspace"], Path] = Field(default_factory=dict, max_length=1)
    max_result_bytes: StrictInt = Field(default=MAX_RESULT_BYTES, ge=128, le=MAX_RESULT_BYTES)
    execution_timeout_seconds: StrictInt = Field(default=10, ge=1, le=10)
    max_receipts: StrictInt = Field(default=10000, ge=1, le=10000)

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        local_directory(self.data_dir)
        for path in self.roots.values():
            local_directory(path)
        return self
