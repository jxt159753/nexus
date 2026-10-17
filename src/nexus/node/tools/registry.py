from dataclasses import dataclass
from types import MappingProxyType

from nexus.contracts.messages import CommandExecute
from nexus.contracts.tools import (
    ListDirectoryCall, ListDirectoryResult, ToolResult,
    TOOL_ARGUMENT_MODELS, TOOL_RESULT_MODELS,
)
from nexus.node.config import NodeConfig
from nexus.node.tools.list_directory import list_directory
from nexus.node.tools.system_info import system_info


@dataclass(frozen=True)
class ToolSpec:
    key: str
    read_only: bool = True
    retry_safe: bool = True
    verification: str = "node_report_schema_checked"


REGISTRY = MappingProxyType({key: ToolSpec(key) for key in TOOL_ARGUMENT_MODELS})


def validate_result(command: CommandExecute, result: ToolResult, max_bytes: int) -> ToolResult:
    model = TOOL_RESULT_MODELS[f"{command.tool}@{command.tool_version}"]
    checked = model.model_validate_json(result.model_dump_json())
    if len(checked.model_dump_json().encode("utf-8")) > max_bytes:
        raise ValueError("result exceeds local byte budget")
    if isinstance(checked, ListDirectoryResult):
        if (not isinstance(command, ListDirectoryCall)
                or checked.root_id != command.arguments.root_id
                or len(checked.entries) > command.arguments.limit):
            raise ValueError("directory evidence does not match command")
    return checked


def execute_tool(command: CommandExecute, config: NodeConfig) -> ToolResult:
    key = f"{command.tool}@{command.tool_version}"
    if key not in REGISTRY or key not in config.allowed_tools:
        raise PermissionError("tool not allowed")
    if isinstance(command, ListDirectoryCall):
        root = config.roots.get(command.arguments.root_id)
        if root is None:
            raise PermissionError("resource not configured")
        result = list_directory(command.arguments, root, config.max_result_bytes)
    else:
        result = system_info()
    return validate_result(command, result, config.max_result_bytes)
