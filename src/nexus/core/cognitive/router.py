
from dataclasses import dataclass
from typing import Literal

from nexus.contracts.errors import ErrorCode
from nexus.contracts.task import (
    ListDirectoryRequest,
    SystemInfoRequest,
    TaskRequest,
)


RoutingMode = Literal["rule", "reject"]


@dataclass(frozen=True)
class RoutingDecision:
    mode: RoutingMode
    reason_code: str
    policy_version: int
    tool_name: str | None
    tool_version: int | None
    arguments: dict[str, object] | None
    error_code: ErrorCode | None = None


def route_task(
    request: TaskRequest,
) -> RoutingDecision:
    if isinstance(request, SystemInfoRequest):
        return RoutingDecision(
            mode="rule",
            reason_code="KNOWN_SYSTEM_INFO",
            policy_version=1,
            tool_name="system.info",
            tool_version=1,
            arguments=request.arguments.model_dump(),
        )

    if isinstance(request, ListDirectoryRequest):
        return RoutingDecision(
            mode="rule",
            reason_code="KNOWN_DIRECTORY_LIST",
            policy_version=1,
            tool_name="filesystem.list",
            tool_version=1,
            arguments=request.arguments.model_dump(),
        )

    return RoutingDecision(
        mode="reject",
        reason_code="UNKNOWN_INTENT",
        policy_version=1,
        tool_name=None,
        tool_version=None,
        arguments=None,
        error_code=ErrorCode.UNSUPPORTED_TASK,
    )
