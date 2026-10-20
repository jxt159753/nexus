from dataclasses import dataclass
from types import MappingProxyType

from nexus.contracts.messages import CommandExecute
from nexus.contracts.tools import (
    ListDirectoryCall, ToolResult, TOOL_ARGUMENT_MODELS, validate_result,
)
from nexus.node.config import NodeConfig
from nexus.node.tools.list_directory import list_directory
from nexus.node.tools.system_info import system_info
from nexus.node.tools.files import read_file, find_files, preview_write, write_file
from nexus.node.tools.commands import run_command


@dataclass(frozen=True)
class ToolSpec:
    key: str
    read_only: bool = True
    retry_safe: bool = True
    verification: str = "node_report_schema_checked"


REGISTRY = MappingProxyType({key: ToolSpec(key, read_only=key not in {"filesystem.write@1","command.run@1"}, retry_safe=key not in {"filesystem.write@1","command.run@1"}) for key in TOOL_ARGUMENT_MODELS})


def execute_tool(command: CommandExecute, config: NodeConfig) -> ToolResult:
    key = f"{command.tool}@{command.tool_version}"
    if key not in REGISTRY or key not in config.allowed_tools:
        raise PermissionError("tool not allowed")
    if command.tool != "system.info":
        root = config.roots.get(command.arguments.root_id)
        if root is None:
            raise PermissionError("resource not configured")
        if command.tool == "filesystem.list":
            result = list_directory(command.arguments, root, config.max_result_bytes)
        elif command.tool == "command.run":
            result = run_command(command.arguments, root, config.commands)
        else:
            result = {"filesystem.read":read_file,"filesystem.search":find_files,"filesystem.write_preview":preview_write,"filesystem.write":write_file}[command.tool](command.arguments, root)
    else:
        result = system_info()
    return validate_result(command, result, config.max_result_bytes)
