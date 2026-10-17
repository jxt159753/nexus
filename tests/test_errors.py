
import pytest
from pydantic import ValidationError

from nexus.contracts.errors import (
    ErrorCode,
    ErrorPayload,
    create_error,
)


def test_create_error():
    error = create_error(
        ErrorCode.UNSUPPORTED_TASK,
        "Unsupported task intent",
    )

    assert error.code == ErrorCode.UNSUPPORTED_TASK
    assert error.retryable is False


def test_unknown_error_code():
    with pytest.raises(ValidationError):
        ErrorPayload(
            code="MADE_UP_ERROR",
            message="Unknown",
            retryable=False,
        )


def test_empty_error_message():
    with pytest.raises(ValidationError):
        create_error(
            ErrorCode.INVALID_MESSAGE,
            "",
        )


def test_invalid_retryable_type():
    with pytest.raises(ValidationError):
        ErrorPayload(
            code=ErrorCode.BUSY,
            message="System busy",
            retryable="yes",
        )
