
from enum import Enum

from pydantic import Field, StrictBool, StrictStr

from nexus.contracts.base import StrictContract


class ErrorCode(str, Enum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    INVALID_MESSAGE = "INVALID_MESSAGE"
    PROTOCOL_UNSUPPORTED = "PROTOCOL_UNSUPPORTED"
    UNSUPPORTED_TASK = "UNSUPPORTED_TASK"
    POLICY_DENIED = "POLICY_DENIED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    NODE_OFFLINE = "NODE_OFFLINE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    RESULT_INVALID = "RESULT_INVALID"
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"
    BUSY = "BUSY"
    MODEL_DISABLED = "MODEL_DISABLED"
    ALREADY_FINISHED = "ALREADY_FINISHED"
    VOICE_DISABLED = "VOICE_DISABLED"
    VOICE_BUSY = "VOICE_BUSY"
    VOICE_CAPABILITY_MISSING = "VOICE_CAPABILITY_MISSING"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    VOICE_PROVIDER_ERROR = "VOICE_PROVIDER_ERROR"


class ErrorPayload(StrictContract):
    code: ErrorCode
    message: StrictStr = Field(min_length=1, max_length=500)
    retryable: StrictBool


def create_error(
    code: ErrorCode,
    message: str,
    retryable: bool = False,
) -> ErrorPayload:
    return ErrorPayload(
        code=code,
        message=message,
        retryable=retryable,
    )


class ProtocolError(ValueError):
    """A bounded public error, without rejected input or validation internals."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        self.error = create_error(code, message)
        super().__init__(f"{code.value}: {message}")
