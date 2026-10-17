import copy
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus.contracts.base import MAX_MESSAGE_BYTES, MAX_RESULT_BYTES
from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.protocol import MESSAGE_ADAPTER, encode_message, parse_message
from nexus.contracts.tools import ListDirectoryResult
from nexus.core.cognitive.router import route_task

IDENTITY = str(uuid4())
DIGEST = "a" * 64
TIME = "2026-10-17T08:00:00Z"
SYSTEM_RESULT = {
    "os_name": "Windows", "os_version": "11",
    "architecture": "AMD64", "python_version": "3.13.9",
}
ERROR = {"code": "EXECUTION_UNKNOWN", "message": "Unknown execution", "retryable": False}
ACTION = {"tool": "system.info", "tool_version": 1, "arguments": {}}
DIRECTORY_ACTION = {
    "tool": "filesystem.list", "tool_version": 1,
    "arguments": {"root_id": "workspace", "limit": 100},
}
COMMAND = {
    "session_id": IDENTITY, "task_id": IDENTITY, "attempt_id": IDENTITY,
    "command_digest": DIGEST, "grant_id": IDENTITY,
    "expires_at": TIME, "execution_timeout_seconds": 10, **ACTION,
}
RESULT = {
    "session_id": IDENTITY, "task_id": IDENTITY, "attempt_id": IDENTITY,
    "command_digest": DIGEST, "tool": "system.info", "tool_version": 1,
    "observed_at": TIME, "outcome": "succeeded", "result": SYSTEM_RESULT,
}
RECEIPT = {
    "session_id": IDENTITY, "task_id": IDENTITY, "attempt_id": IDENTITY,
    "command_digest": DIGEST, "boot_id": IDENTITY,
}
PAYLOADS = {
    "client.sync": {"event_id": None},
    "task.submit": {"request_id": IDENTITY, "intent": "get_system_info", "arguments": {}},
    "task.cancel": {"task_id": IDENTITY},
    "approval.decide": {"approval_id": IDENTITY, "decision": "approve"},
    "control.pause": {"request_id": IDENTITY},
    "control.resume": {"request_id": IDENTITY},
    "control.cancel_all": {"request_id": IDENTITY},
    "client.synced": {"event_id": "10", "full_sync_required": False},
    "task.accepted": {"task_id": IDENTITY, "status": "queued"},
    "task.updated": {
        "event_id": "11", "task_id": IDENTITY, "revision": 1,
        "status": "succeeded", "execution_outcome": "known",
        "result": SYSTEM_RESULT, "verification": "node_report_schema_checked",
    },
    "approval.required": {
        "event_id": "12", "approval_id": IDENTITY, "task_id": IDENTITY,
        "node_id": IDENTITY, "action": DIRECTORY_ACTION, "expires_at": TIME,
    },
    "control.updated": {"event_id": "13", "paused": True, "revision": 2},
    "node.hello": {
        "node_id": IDENTITY, "node_version": "0.1.0", "boot_id": IDENTITY,
        "tools": [{"tool": "system.info", "tool_version": 1}], "roots": ["workspace"],
    },
    "node.welcome": {
        "session_id": IDENTITY, "heartbeat_interval_seconds": 10,
        "heartbeat_timeout_seconds": 30, "limits": {
            "max_message_bytes": MAX_MESSAGE_BYTES, "max_result_bytes": MAX_RESULT_BYTES,
            "execution_timeout_seconds": 10, "max_concurrent_attempts": 1,
        },
    },
    "node.heartbeat": {"session_id": IDENTITY, "active_attempt_id": None},
    "command.execute": COMMAND,
    "command.accepted": RECEIPT,
    "command.started": RECEIPT,
    "command.status": {"session_id": IDENTITY, "attempt_id": IDENTITY},
    "command.status_result": {
        "session_id": IDENTITY, "attempt_id": IDENTITY, "phase": "completed",
        "command_digest": DIGEST, "result": RESULT,
    },
    "command.cancel": {"session_id": IDENTITY, "attempt_id": IDENTITY, "reason": "Cancelled"},
    "command.cancelled": {"session_id": IDENTITY, "attempt_id": IDENTITY, "stopped_phase": "started"},
    "command.result": RESULT,
    "result.ack": {"session_id": IDENTITY, "attempt_id": IDENTITY, "status": "stored"},
    "error": {**ERROR, "session_id": IDENTITY},
}


def message(kind="task.submit"):
    return {
        "protocol_version": 1, "type": kind, "message_id": str(uuid4()),
        "correlation_id": None, "sent_at": TIME, "payload": copy.deepcopy(PAYLOADS[kind]),
    }


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def test_protocol_type_coverage():
    assert set(MESSAGE_ADAPTER.json_schema()["discriminator"]["mapping"]) == set(PAYLOADS)


@pytest.mark.parametrize("kind", PAYLOADS)
def test_all_message_types_round_trip(kind):
    original = message(kind)
    parsed = parse_message(wire(original))
    assert parsed.type == kind
    assert parse_message(encode_message(parsed)) == parsed
    assert parsed.payload.model_dump(mode="json", exclude_unset=True) == original["payload"]


@pytest.mark.parametrize("kind", PAYLOADS)
@pytest.mark.parametrize("location", ["envelope", "payload"])
def test_all_message_types_reject_unknown_fields(kind, location):
    original = message(kind)
    target = original if location == "envelope" else original["payload"]
    target["unexpected"] = "not allowed"
    with pytest.raises(ProtocolError) as exc:
        parse_message(wire(original))
    assert exc.value.error.code == ErrorCode.INVALID_MESSAGE


@pytest.mark.parametrize("version", [True, False, 1.0, "1", 0, 2, None])
def test_codec_rejects_protocol_versions(version):
    original = message()
    original["protocol_version"] = version
    with pytest.raises(ProtocolError) as exc:
        parse_message(wire(original))
    assert exc.value.error.code == ErrorCode.PROTOCOL_UNSUPPORTED


@pytest.mark.parametrize("raw", [b"{", b"[]", b"null", b"\xff", b'"secret"', "\ud800"])
def test_invalid_json_is_bounded_public_error(raw):
    with pytest.raises(ProtocolError) as exc:
        parse_message(raw)
    assert exc.value.error.code == ErrorCode.INVALID_MESSAGE
    assert "secret" not in str(exc.value)
    assert exc.value.error.retryable is False


def test_unknown_message_type_is_rejected():
    original = message()
    original["type"] = "shell.execute"
    with pytest.raises(ProtocolError) as exc:
        parse_message(wire(original))
    assert exc.value.error.code == ErrorCode.INVALID_MESSAGE


def test_unknown_intent_returns_unsupported_without_network(monkeypatch):
    def forbidden_network(*args, **kwargs):
        pytest.fail("rule routing must not open a network connection")

    monkeypatch.setattr("socket.socket", forbidden_network)
    monkeypatch.setattr("socket.create_connection", forbidden_network)
    for intent, arguments, tool in (
        ("get_system_info", {}, "system.info"),
        ("list_directory", {"root_id": "workspace", "limit": 100}, "filesystem.list"),
    ):
        original = message()
        original["payload"].update(intent=intent, arguments=arguments)
        decision = route_task(parse_message(wire(original)).payload)
        assert (decision.mode, decision.tool_name) == ("rule", tool)
    original["payload"]["intent"] = "run_shell"
    with pytest.raises(ProtocolError) as exc:
        parse_message(wire(original))
    assert exc.value.error.code == ErrorCode.UNSUPPORTED_TASK


@pytest.mark.parametrize("value", [True, 1.0, "1", 2])
def test_tool_version_is_strict(value):
    original = message("command.execute")
    original["payload"]["tool_version"] = value
    with pytest.raises(ProtocolError):
        parse_message(wire(original))


@pytest.mark.parametrize("kind", ["command.execute", "command.result"])
def test_tool_and_data_must_match(kind):
    original = message(kind)
    original["payload"]["tool"] = "filesystem.list"
    with pytest.raises(ProtocolError):
        parse_message(wire(original))


@pytest.mark.parametrize("kind", ["command.execute", "command.result"])
def test_directory_tool_data_is_accepted(kind):
    original = message(kind)
    original["payload"]["tool"] = "filesystem.list"
    if kind == "command.execute":
        original["payload"]["arguments"] = DIRECTORY_ACTION["arguments"]
    else:
        original["payload"]["result"] = {"root_id": "workspace", "entries": [], "truncated": False}
    assert parse_message(wire(original)).payload.tool == "filesystem.list"


@pytest.mark.parametrize("changes", [
    {"result": None}, {"error": ERROR}, {"outcome": "failed"},
    {"outcome": "failed", "error": ERROR},
])
def test_result_and_error_are_exclusive(changes):
    original = message("command.result")
    original["payload"].update(changes)
    with pytest.raises(ProtocolError):
        parse_message(wire(original))


def test_failed_tool_result_is_accepted():
    original = message("command.result")
    original["payload"].update(outcome="failed", result=None, error=ERROR)
    assert parse_message(wire(original)).payload.outcome == "failed"


@pytest.mark.parametrize("field", ["session_id", "command_digest", "attempt_id"])
def test_status_result_cannot_change_identity(field):
    original = message("command.status_result")
    original["payload"]["result"][field] = "b" * 64 if field == "command_digest" else str(uuid4())
    with pytest.raises(ProtocolError):
        parse_message(wire(original))


@pytest.mark.parametrize("phase", ["not_found", "received", "started", "cancelled", "unknown"])
def test_status_without_completed_result(phase):
    original = message("command.status_result")
    original["payload"].update(phase=phase, result=None)
    if phase == "not_found":
        original["payload"]["command_digest"] = None
    assert parse_message(wire(original)).payload.phase == phase


@pytest.mark.parametrize("changes", [
    {"phase": "not_found"}, {"phase": "completed", "result": None},
    {"phase": "started"}, {"command_digest": None},
])
def test_status_evidence_shape_is_checked(changes):
    original = message("command.status_result")
    original["payload"].update(changes)
    with pytest.raises(ProtocolError):
        parse_message(wire(original))


@pytest.mark.parametrize("changes", [
    {"execution_outcome": "unknown"}, {"verification": None}, {"result": None},
])
def test_task_success_requires_known_verified_evidence(changes):
    original = message("task.updated")
    original["payload"].update(changes)
    with pytest.raises(ProtocolError):
        parse_message(wire(original))


@pytest.mark.parametrize("timestamp", [
    "2026-10-17T08:00:00", "2026-10-17T16:00:00+08:00",
    1792224000, "1792224000", "2026-10-17 08:00:00Z",
])
def test_utc_rfc3339_is_required(timestamp):
    original = message()
    original["sent_at"] = timestamp
    with pytest.raises(ProtocolError):
        parse_message(wire(original))


def test_utf8_message_byte_limit():
    raw = wire(message())
    padded = raw + " " * (MAX_MESSAGE_BYTES - len(raw.encode("utf-8")))
    assert parse_message(padded).type == "task.submit"
    assert parse_message(padded.encode("utf-8")).type == "task.submit"
    for oversized in (padded + " ", (padded + " ").encode("utf-8")):
        with pytest.raises(ProtocolError):
            parse_message(oversized)
    original = message("command.cancel")
    original["payload"]["reason"] = "\u4e2d" * 500
    raw = wire(original)
    padded = raw + " " * (MAX_MESSAGE_BYTES - len(raw))
    assert len(padded) == MAX_MESSAGE_BYTES
    assert len(padded.encode("utf-8")) > MAX_MESSAGE_BYTES
    with pytest.raises(ProtocolError):
        parse_message(padded)


def directory_result_bytes(size):
    result = {"root_id": "workspace", "entries": [{"name": "x", "kind": "file"} for _ in range(120)], "truncated": True}
    remaining = size - len(wire(result).encode("utf-8"))
    for entry in result["entries"]:
        count = min(remaining, 254)
        entry["name"] += "x" * count
        remaining -= count
    assert remaining == 0
    return wire(result)


def test_result_byte_limit_boundary():
    accepted = directory_result_bytes(MAX_RESULT_BYTES)
    assert len(accepted.encode("utf-8")) == MAX_RESULT_BYTES
    assert ListDirectoryResult.model_validate_json(accepted).truncated is True
    rejected = directory_result_bytes(MAX_RESULT_BYTES + 1)
    with pytest.raises(ValidationError, match="32 KiB"):
        ListDirectoryResult.model_validate_json(rejected)


def test_original_multibyte_result_regression():
    result = {"root_id": "workspace", "entries": [{"name": "\u4e2d" * 255, "kind": "file"} for _ in range(200)], "truncated": True}
    with pytest.raises(ValidationError, match="32 KiB"):
        ListDirectoryResult.model_validate_json(wire(result))


def test_encoder_revalidates_changed_messages():
    parsed = parse_message(wire(message("command.execute")))
    parsed.payload.arguments = {"shell": "not allowed"}
    with pytest.raises(ProtocolError):
        encode_message(parsed)


@pytest.mark.parametrize("kind", [
    "node.welcome", "node.heartbeat", "command.execute", "command.accepted",
    "command.started", "command.status", "command.status_result", "command.cancel",
    "command.cancelled", "command.result", "result.ack",
])
def test_node_runtime_messages_require_session_id(kind):
    original = message(kind)
    del original["payload"]["session_id"]
    with pytest.raises(ProtocolError) as exc:
        parse_message(wire(original))
    assert exc.value.error.code == ErrorCode.INVALID_MESSAGE


@pytest.mark.parametrize("value", [0, 201, True, 1.0, "100"])
def test_directory_limit_is_strict_at_wire_boundary(value):
    original = message("command.execute")
    original["payload"].update(copy.deepcopy(DIRECTORY_ACTION))
    original["payload"]["arguments"]["limit"] = value
    with pytest.raises(ProtocolError):
        parse_message(wire(original))


@pytest.mark.parametrize("value", ["C:\\Users", "\\\\server\\share", "../docs", "/tmp", ""])
def test_directory_alias_rejects_paths(value):
    original = message("command.execute")
    original["payload"].update(copy.deepcopy(DIRECTORY_ACTION))
    original["payload"]["arguments"]["root_id"] = value
    with pytest.raises(ProtocolError):
        parse_message(wire(original))


def test_nested_extra_version_field_is_not_protocol_version_error():
    original = message()
    original["payload"]["protocol_version"] = 2
    with pytest.raises(ProtocolError) as exc:
        parse_message(wire(original))
    assert exc.value.error.code == ErrorCode.INVALID_MESSAGE


@pytest.mark.parametrize("changes", [
    {"command_digest": "invalid"}, {"execution_timeout_seconds": 11},
    {"execution_timeout_seconds": True}, {"tool": "shell.execute"},
    {"arguments": {"shell": "not allowed"}},
])
def test_command_action_bounds(changes):
    original = message("command.execute")
    original["payload"].update(changes)
    with pytest.raises(ProtocolError):
        parse_message(wire(original))


@pytest.mark.parametrize("changes", [
    {"heartbeat_timeout_seconds": 10},
    {"limits": {**PAYLOADS["node.welcome"]["limits"], "max_result_bytes": MAX_RESULT_BYTES + 1}},
    {"limits": {**PAYLOADS["node.welcome"]["limits"], "max_message_bytes": MAX_MESSAGE_BYTES + 1}},
    {"limits": {**PAYLOADS["node.welcome"]["limits"], "max_concurrent_attempts": 2}},
])
def test_node_limits_cannot_expand_design_budget(changes):
    original = message("node.welcome")
    original["payload"].update(changes)
    with pytest.raises(ProtocolError):
        parse_message(wire(original))


def test_duplicate_capability_is_rejected():
    original = message("node.hello")
    original["payload"]["tools"] *= 2
    with pytest.raises(ProtocolError):
        parse_message(wire(original))


def test_invalid_unicode_result_is_public_error():
    original = message("command.result")
    original["payload"]["result"]["os_name"] = "\ud800"
    with pytest.raises(ProtocolError) as exc:
        parse_message(json.dumps(original))
    assert exc.value.error.code == ErrorCode.INVALID_MESSAGE
