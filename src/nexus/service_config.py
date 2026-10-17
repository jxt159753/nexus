"""Bounded TOML and environment loading, with no application imports."""

import json
import os
import tomllib
from pathlib import Path
from typing import TypeVar
from nexus.contracts.base import MAX_MESSAGE_BYTES, StrictContract

Config = TypeVar("Config", bound=StrictContract)


def load_config(path: Path, model: type[Config], overrides: dict[str, str]) -> Config:
    with path.open("rb") as source:
        raw = source.read(MAX_MESSAGE_BYTES + 1)
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("configuration exceeds 64 KiB")
    value = tomllib.loads(raw.decode("utf-8"))
    for variable, field in overrides.items():
        if variable in os.environ:
            value[field] = int(os.environ[variable]) if field == "port" else os.environ[variable]
    return model.model_validate_json(json.dumps(value))
