from dataclasses import dataclass
from types import MappingProxyType

from nexus.contracts.basic import MUTATING_TOOLS
from nexus.node.tools.workspace import read_lines, glob_files, grep_files, preview_edit, edit_file
from nexus.contracts.messages import CommandExecute
from nexus.contracts.tools import (
    ListDirectoryCall, ToolResult, TOOL_ARGUMENT_MODELS, validate_result,
)
from nexus.node.config import NodeConfig
from nexus.node.tools.list_directory import list_directory
from nexus.node.tools.system_info import system_info
from nexus.node.tools.files import read_file, find_files, preview_write, write_file
from nexus.node.tools.commands import run_command
from nexus.node.tools.images import read_image


@dataclass(frozen=True)
class ToolSpec:
    key: str
    read_only: bool = True
    retry_safe: bool = True
    verification: str = "node_report_schema_checked"


REGISTRY = MappingProxyType({key: ToolSpec(key, read_only=key.rsplit("@",1)[0] not in MUTATING_TOOLS, retry_safe=key.rsplit("@",1)[0] not in MUTATING_TOOLS) for key in TOOL_ARGUMENT_MODELS})


def execute_tool(command: CommandExecute, config: NodeConfig) -> ToolResult:
    key = f"{command.tool}@{command.tool_version}"
    if key not in REGISTRY or key not in config.allowed_tools:
        raise PermissionError("tool not allowed")
    if command.tool != "system.info":
        root = config.roots.get(command.arguments.root_id)
        if root is None:
            raise PermissionError("resource not configured")
        if command.tool.startswith("interaction."):
            target=config.interaction_targets.get(command.arguments.target_id)
            if target is None:
                raise PermissionError("interaction target not configured")
            from nexus.node.tools.browser import browser_operation
            from nexus.node.tools.apps import app_operation
            result=(app_operation if target.kind=="windows_app" else browser_operation)(command.arguments,target)
        elif command.tool == "filesystem.list":
            result = list_directory(command.arguments, root, config.max_result_bytes)
        elif command.tool == "command.run":
            result = run_command(command.arguments, root, config.commands)
        elif command.tool == "filesystem.read_image":
            result = read_image(command.arguments, root)
        else:
            result = {"filesystem.read_lines":read_lines,"filesystem.glob":glob_files,"filesystem.grep":grep_files,"filesystem.edit_preview":preview_edit,"filesystem.edit":edit_file,"filesystem.read":read_file,"filesystem.search":find_files,"filesystem.write_preview":preview_write,"filesystem.write":write_file}[command.tool](command.arguments, root)
    else:
        result = system_info()
    return validate_result(command, result, config.max_result_bytes)
