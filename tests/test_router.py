
from uuid import uuid4

from nexus.contracts.errors import ErrorCode
from nexus.contracts.task import (
    ListDirectoryArguments,
    ListDirectoryRequest,
    SystemInfoArguments,
    SystemInfoRequest,
)
from nexus.core.cognitive.router import route_task


def test_system_info_routing():
    request = SystemInfoRequest(
        request_id=uuid4(),
        intent="get_system_info",
        arguments=SystemInfoArguments(),
    )

    decision = route_task(request)

    assert decision.mode == "rule"
    assert decision.tool_name == "system.info"
    assert decision.tool_version == 1
    assert decision.arguments == {}
    assert decision.error_code is None


def test_directory_routing():
    request = ListDirectoryRequest(
        request_id=uuid4(),
        intent="list_directory",
        arguments=ListDirectoryArguments(
            root_id="workspace",
            limit=50,
        ),
    )

    decision = route_task(request)

    assert decision.mode == "rule"
    assert decision.tool_name == "filesystem.list"
    assert decision.tool_version == 1
    assert decision.arguments == {
        "root_id": "workspace",
        "limit": 50,
    }


def test_unknown_request_is_rejected():
    decision = route_task(None)

    assert decision.mode == "reject"
    assert decision.tool_name is None
    assert decision.error_code == ErrorCode.UNSUPPORTED_TASK


def test_routing_is_deterministic():
    request = SystemInfoRequest(
        request_id=uuid4(),
        intent="get_system_info",
        arguments=SystemInfoArguments(),
    )

    first = route_task(request)
    second = route_task(request)

    assert first == second


def test_routing_has_no_model_mode():
    request = SystemInfoRequest(
        request_id=uuid4(),
        intent="get_system_info",
        arguments=SystemInfoArguments(),
    )

    decision = route_task(request)

    assert decision.mode in ("rule", "reject")
