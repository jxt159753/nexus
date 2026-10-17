"""Fixed worker. EOF without an input gate never runs a tool."""

import json
import sys
from datetime import UTC, datetime

from pydantic import TypeAdapter

from nexus.contracts.base import MAX_MESSAGE_BYTES, StrictContract
from nexus.contracts.errors import ErrorCode, create_error
from nexus.contracts.messages import CommandExecute, CommandResult
from nexus.node.config import NodeConfig


class WorkerRequest(StrictContract):
    command: CommandExecute
    config: NodeConfig
    job_name: str


RESULT = TypeAdapter(CommandResult)


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
    if not raw or len(raw) > MAX_MESSAGE_BYTES:
        return 2
    try:
        request = WorkerRequest.model_validate_json(raw)
    except ValueError:
        return 2
    from nexus.node.windows import require_current_job
    try:
        require_current_job(request.job_name)
    except OSError:
        return 2
    # This import and every tool observation occur only after the parent opens the gate.
    from nexus.node.tools.registry import execute_tool

    command = request.command
    value = command.model_dump(mode="json", include={
        "session_id", "task_id", "attempt_id", "command_digest", "tool", "tool_version",
    })
    try:
        result = execute_tool(command, request.config)
        value.update(outcome="succeeded", result=result.model_dump(mode="json"))
    except PermissionError:
        value.update(outcome="failed", error=create_error(ErrorCode.POLICY_DENIED, "Local resource access denied").model_dump(mode="json"))
    except (OSError, ValueError):
        value.update(outcome="failed", error=create_error(ErrorCode.RESULT_INVALID, "Read-only tool observation failed").model_dump(mode="json"))
    value["observed_at"] = datetime.now(UTC).isoformat()
    encoded = RESULT.validate_json(json.dumps(value)).model_dump_json().encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        return 2
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
