
from uuid import uuid4
import json

import pytest
from pydantic import TypeAdapter, ValidationError

from nexus.contracts.task import TaskRequest
from nexus.contracts.tools import (
    ListDirectoryResult,
    SystemInfoResult,
)


task_adapter = TypeAdapter(TaskRequest)


def test_system_info_request():
    request = task_adapter.validate_json(
        json.dumps({
            "request_id": str(uuid4()),
            "intent": "get_system_info",
            "arguments": {},
        })
    )

    assert request.intent == "get_system_info"


def test_directory_limit():
    request = {
        "request_id": str(uuid4()),
        "intent": "list_directory",
        "arguments": {
            "root_id": "workspace",
            "limit": 201,
        },
    }

    with pytest.raises(ValidationError) as exc:
        task_adapter.validate_json(json.dumps(request))
    assert [(item["loc"], item["type"]) for item in exc.value.errors()] == [
        (("list_directory", "arguments", "limit"), "less_than_equal")
    ]


def test_unknown_intent():
    request = {
        "request_id": str(uuid4()),
        "intent": "delete_files",
        "arguments": {},
    }

    with pytest.raises(ValidationError) as exc:
        task_adapter.validate_json(json.dumps(request))
    assert [item["type"] for item in exc.value.errors()] == ["union_tag_invalid"]


def test_extra_field():
    request = {
        "request_id": str(uuid4()),
        "intent": "get_system_info",
        "arguments": {},
        "admin": True,
    }

    with pytest.raises(ValidationError) as exc:
        task_adapter.validate_json(json.dumps(request))
    assert [(item["loc"], item["type"]) for item in exc.value.errors()] == [
        (("get_system_info", "admin"), "extra_forbidden")
    ]


@pytest.mark.parametrize("limit", [1, 100, 200])
def test_valid_directory_limits(limit):
    request = task_adapter.validate_json(json.dumps({
        "request_id": str(uuid4()),
        "intent": "list_directory",
        "arguments": {"root_id": "workspace", "limit": limit},
    }))
    assert request.arguments.limit == limit


def test_system_info_result():
    result = SystemInfoResult.model_validate({
        "os_name": "Windows",
        "os_version": "11",
        "architecture": "AMD64",
        "python_version": "3.13.9",
    })

    assert result.os_name == "Windows"


def test_directory_result():
    result = ListDirectoryResult.model_validate({
        "root_id": "workspace",
        "entries": [
            {"name": "docs", "kind": "directory"},
        ],
        "truncated": False,
    })

    assert len(result.entries) == 1


def test_directory_result_rejects_extra_fields():
    with pytest.raises(ValidationError):
        ListDirectoryResult.model_validate({
            "root_id": "workspace",
            "entries": [],
            "truncated": False,
            "absolute_path": "C:\\Users",
        })


def test_directory_result_rejects_too_many_entries():
    with pytest.raises(ValidationError):
        ListDirectoryResult.model_validate({
            "root_id": "workspace",
            "entries": [
                {"name": f"file_{i}", "kind": "file"}
                for i in range(201)
            ],
            "truncated": True,
        })
