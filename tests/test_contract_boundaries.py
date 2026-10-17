
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus.contracts.protocol import TaskSubmitEnvelope


def make_message(intent="get_system_info", arguments=None):
    return {
        "protocol_version": 1,
        "type": "task.submit",
        "message_id": str(uuid4()),
        "correlation_id": None,
        "sent_at": "2026-10-17T08:00:00Z",
        "payload": {
            "request_id": str(uuid4()),
            "intent": intent,
            "arguments": arguments if arguments is not None else {},
        },
    }


def test_valid_json_message():
    message = make_message()

    parsed = TaskSubmitEnvelope.model_validate_json(
        json.dumps(message)
    )

    assert parsed.protocol_version == 1


@pytest.mark.parametrize("version", [2, 0, -1, True, False, 1.0, "1", None])
def test_protocol_version_rejected(version):
    message = make_message()
    message["protocol_version"] = version

    with pytest.raises(ValidationError) as exc:
        TaskSubmitEnvelope.model_validate_json(
            json.dumps(message)
        )

    assert any(
        error["loc"] == ("protocol_version",)
        for error in exc.value.errors()
    )


def test_unknown_intent_rejected():
    message = make_message(intent="run_shell")

    with pytest.raises(ValidationError):
        TaskSubmitEnvelope.model_validate_json(
            json.dumps(message)
        )


def test_arbitrary_path_rejected():
    message = make_message(
        intent="list_directory",
        arguments={
            "root_id": "C:\\Users",
            "limit": 50,
        },
    )

    with pytest.raises(ValidationError):
        TaskSubmitEnvelope.model_validate_json(
            json.dumps(message)
        )


def test_boolean_limit_rejected():
    message = make_message(
        intent="list_directory",
        arguments={
            "root_id": "workspace",
            "limit": True,
        },
    )

    with pytest.raises(ValidationError):
        TaskSubmitEnvelope.model_validate_json(
            json.dumps(message)
        )
