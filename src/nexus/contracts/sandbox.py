"""Explicit JARVIS Link v2 sandbox protocol; separate from the v1 message union.

Only authenticated Sandbox Nodes that completed a v2 handshake receive these
messages. Unknown protocol versions, types or fields are rejected.
"""

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, ValidationError, model_validator

from nexus.contracts.base import StrictContract
from nexus.contracts.errors import ErrorCode, ProtocolError

MAX_V2_BYTES = 192 * 1024
DIGEST = r"^[0-9a-f]{64}$"


class SandboxHello(StrictContract):
    node_id: UUID
    node_version: StrictStr = Field(min_length=1, max_length=50)
    boot_id: UUID
    runtime: Literal["runsc"]
    platform: Literal["systrap", "kvm"] = "systrap"
    runsc_digest: StrictStr | None = Field(default=None, pattern=DIGEST)
    runtime_version: StrictStr = Field(min_length=1, max_length=200)
    image_digest: StrictStr = Field(pattern=DIGEST)
    available: StrictBool
    reason: StrictStr | None = Field(default=None, max_length=300)
    verification_profiles: list[Literal["directory_entry_stats_v1"]] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def consistent(self):
        if self.available and self.reason is not None:
            raise ValueError("available sandbox cannot carry an unavailable reason")
        if not self.available and not self.reason:
            raise ValueError("unavailable sandbox must state a reason")
        return self


class SandboxWelcome(StrictContract):
    session_id: UUID
    heartbeat_interval_seconds: StrictInt = Field(ge=1, le=10)
    heartbeat_timeout_seconds: StrictInt = Field(ge=2, le=30)
    max_message_bytes: StrictInt = Field(ge=1024, le=MAX_V2_BYTES)
    max_result_bytes: StrictInt = Field(ge=128, le=32 * 1024)
    execution_timeout_seconds: StrictInt = Field(ge=1, le=10)


class SandboxHeartbeat(StrictContract):
    session_id: UUID
    active_execution_id: UUID | None = None


class SandboxExecute(StrictContract):
    session_id: UUID
    execution_id: UUID
    version_id: UUID
    artifact_digest: StrictStr = Field(pattern=DIGEST)
    environment_digest: StrictStr = Field(pattern=DIGEST)
    release_json: StrictStr = Field(min_length=1, max_length=70000)
    input_json: StrictStr = Field(min_length=1, max_length=20000)
    timeout_seconds: StrictInt = Field(ge=1, le=10)


class SandboxResult(StrictContract):
    session_id: UUID
    execution_id: UUID
    artifact_digest: StrictStr = Field(pattern=DIGEST)
    outcome: Literal["succeeded", "failed", "unknown"]
    output_json: StrictStr | None = Field(default=None, max_length=40000)
    error_code: StrictStr | None = Field(default=None, max_length=80)
    stopped: StrictBool = False

    @model_validator(mode="after")
    def consistent(self):
        if self.outcome == "succeeded" and (self.output_json is None or self.error_code is not None):
            raise ValueError("succeeded requires output only")
        if self.outcome != "succeeded" and (self.output_json is not None or self.error_code is None):
            raise ValueError("failure/unknown requires an error only")
        if self.outcome == "unknown" and self.stopped:
            raise ValueError("unknown cannot claim a confirmed stop")
        return self


class SandboxCancel(StrictContract):
    session_id: UUID
    execution_id: UUID


class SandboxCancelled(StrictContract):
    session_id: UUID
    execution_id: UUID
    stopped: StrictBool


class SandboxStatus(StrictContract):
    session_id: UUID
    execution_id: UUID


class SandboxStatusResult(StrictContract):
    session_id: UUID
    execution_id: UUID
    status: Literal["not_found", "received", "running", "settled", "unknown"]
    command_digest: StrictStr | None = Field(default=None, pattern=DIGEST)
    result: SandboxResult | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "not_found") != (self.command_digest is None):
            raise ValueError("receipt identity required for known execution")
        if self.result is not None and (self.result.execution_id != self.execution_id or self.result.session_id != self.session_id):
            raise ValueError("status result identity mismatch")
        if (self.status == "settled") != (self.result is not None):
            raise ValueError("settled status requires a result")
        return self


class SandboxResultAck(StrictContract):
    session_id: UUID
    execution_id: UUID
    artifact_digest: StrictStr = Field(pattern=DIGEST)


def sandbox_command_digest(version_id, artifact_digest: str, environment_digest: str,
                           input_value: dict, timeout_seconds: int) -> str:
    from nexus.contracts.extensions import digest_value
    return digest_value({"version_id": str(version_id), "artifact_digest": artifact_digest,
                         "environment_digest": environment_digest, "input": input_value,
                         "timeout_seconds": timeout_seconds})


class SandboxEnvelope(StrictContract):
    protocol_version: Literal[2]
    type: Literal["sandbox.hello", "sandbox.welcome", "sandbox.heartbeat", "sandbox.execute",
                  "sandbox.result", "sandbox.cancel", "sandbox.cancelled", "sandbox.status",
                  "sandbox.status_result", "sandbox.result_ack", "error"]
    message_id: UUID
    correlation_id: UUID | None = None
    sent_at: StrictStr = Field(min_length=20, max_length=40)
    payload: dict


PAYLOADS = {
    "sandbox.hello": SandboxHello,
    "sandbox.welcome": SandboxWelcome,
    "sandbox.heartbeat": SandboxHeartbeat,
    "sandbox.execute": SandboxExecute,
    "sandbox.result": SandboxResult,
    "sandbox.cancel": SandboxCancel,
    "sandbox.cancelled": SandboxCancelled,
    "sandbox.status": SandboxStatus,
    "sandbox.status_result": SandboxStatusResult,
    "sandbox.result_ack": SandboxResultAck,
}


def _unique_fields(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate v2 JSON key")
        value[key] = item
    return value


def parse_sandbox_message(data: str | bytes):
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) > MAX_V2_BYTES:
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "v2 message exceeds its frame budget")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_fields,
                           parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except (ValueError, UnicodeError):
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "invalid v2 JSON") from None
    if not isinstance(value, dict) or value.get("protocol_version") != 2:
        raise ProtocolError(ErrorCode.PROTOCOL_UNSUPPORTED, "v2 sandbox negotiation required")
    try:
        envelope = SandboxEnvelope.model_validate_json(raw)
        model = PAYLOADS.get(envelope.type)
        if model is None:
            raise ProtocolError(ErrorCode.INVALID_MESSAGE, "v2 error envelope has no payload schema here")
        payload = model.model_validate_json(json.dumps(envelope.payload))
    except ValidationError:
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "invalid v2 message") from None
    return envelope.model_copy(update={"payload": payload})


def encode_sandbox_message(kind: str, payload: StrictContract, message_id: UUID | None = None,
                           correlation_id: UUID | None = None, sent_at: str | None = None) -> str:
    from datetime import UTC, datetime
    from uuid import uuid4
    value = {
        "protocol_version": 2,
        "type": kind,
        "message_id": str(message_id or uuid4()),
        "correlation_id": str(correlation_id) if correlation_id else None,
        "sent_at": sent_at or datetime.now(UTC).isoformat(),
        "payload": payload.model_dump(mode="json"),
    }
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_V2_BYTES:
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, "v2 frame exceeds its budget")
    return text
