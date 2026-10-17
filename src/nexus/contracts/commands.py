"""Canonical immutable command identity, shared by Core and Node."""

import hashlib
import json
from uuid import UUID

from nexus.contracts.messages import CommandExecute


def command_identity(command: CommandExecute, node_id: UUID) -> str:
    value = command.model_dump(mode="json", exclude={"session_id", "command_digest"})
    value["node_id"] = str(node_id)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def command_digest(command: CommandExecute, node_id: UUID) -> str:
    return hashlib.sha256(command_identity(command, node_id).encode("utf-8")).hexdigest()
