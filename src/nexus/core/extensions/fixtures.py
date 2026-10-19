"""Deterministic development-only candidates for the M4 demonstrations.

This module is never a real model. It is enabled only by an explicit Core
configuration flag and goes through the same parse, digest, test, approval and
enablement path as a model response. It performs no execution.
"""

import json
from uuid import UUID

TOOL_SOURCE = '''import json
import sys

KINDS = ("file", "directory", "link", "other")


def main():
    value = json.load(sys.stdin)
    entries = value["entries"]
    counts = {kind: 0 for kind in KINDS}
    for entry in entries:
        counts[entry["kind"]] += 1
    json.dump({"counts": counts, "total": len(entries)}, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
'''

TOOL_MANIFEST = {
    "kind": "tool",
    "capability_id": "user.directory_entry_stats",
    "description": "Count explicitly supplied directory entries by kind without reading any file.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "maxItems": 200,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "maxLength": 255},
                        "kind": {"type": "string", "enum": ["file", "directory", "link", "other"]},
                    },
                    "required": ["name", "kind"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["entries"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "counts": {
                "type": "object",
                "properties": {
                    "file": {"type": "integer", "minimum": 0, "maximum": 200},
                    "directory": {"type": "integer", "minimum": 0, "maximum": 200},
                    "link": {"type": "integer", "minimum": 0, "maximum": 200},
                    "other": {"type": "integer", "minimum": 0, "maximum": 200},
                },
                "required": ["file", "directory", "link", "other"],
                "additionalProperties": False,
            },
            "total": {"type": "integer", "minimum": 0, "maximum": 200},
        },
        "required": ["counts", "total"],
        "additionalProperties": False,
    },
    "verification_profile": "directory_entry_stats_v1",
    "dependencies": ["python-stdlib"],
}

SKILL_OUTPUT_V1 = {
    "type": "object",
    "properties": {
        "system": {
            "type": "object",
            "properties": {
                "os_name": {"type": "string", "maxLength": 100},
                "os_version": {"type": "string", "maxLength": 200},
                "architecture": {"type": "string", "maxLength": 100},
                "python_version": {"type": "string", "maxLength": 100},
            },
            "required": ["os_name", "os_version", "architecture", "python_version"],
            "additionalProperties": False,
        },
        "directory": {
            "type": "object",
            "properties": {
                "root_id": {"type": "string", "const": "workspace"},
                "entries": {
                    "type": "array",
                    "maxItems": 200,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "maxLength": 255},
                            "kind": {"type": "string", "enum": ["file", "directory", "link", "other"]},
                        },
                        "required": ["name", "kind"],
                        "additionalProperties": False,
                    },
                },
                "truncated": {"type": "boolean"},
            },
            "required": ["root_id", "entries", "truncated"],
            "additionalProperties": False,
        },
    },
    "required": ["system", "directory"],
    "additionalProperties": False,
}


def _skill_v1() -> dict:
    return {
        "kind": "skill",
        "manifest": {
            "kind": "skill",
            "capability_id": "user.workspace_overview",
            "description": "Read bounded system information and then list the workspace root.",
            "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            "output_schema": SKILL_OUTPUT_V1,
            "verification_profile": "skill_steps_v1",
            "dependencies": [],
        },
        "markdown": (
            "# 工作区概览\n\n"
            "1. 读取操作系统、版本、架构和 Python 版本。\n"
            "2. 列出 workspace 的直接条目，保留原有的目录审批。\n\n"
            "只返回已验证结果。目录结果如标记 truncated，不应当作完整目录。"
        ),
        "steps": [
            {"id": "system", "tool": "system.info", "tool_version": 1, "arguments": {}},
            {"id": "directory", "tool": "filesystem.list", "tool_version": 1,
             "arguments": {"root_id": "workspace", "limit": 100}},
        ],
        "tests": [{"name": "two-step overview", "expected_steps": ["system", "directory"]}],
    }


def _skill_v2(tool_version_id: UUID) -> dict:
    output = json.loads(json.dumps(SKILL_OUTPUT_V1))
    output["properties"]["stats"] = {
        "type": "object",
        "properties": {
            "counts": {
                "type": "object",
                "properties": {
                    "file": {"type": "integer", "minimum": 0, "maximum": 200},
                    "directory": {"type": "integer", "minimum": 0, "maximum": 200},
                    "link": {"type": "integer", "minimum": 0, "maximum": 200},
                    "other": {"type": "integer", "minimum": 0, "maximum": 200},
                },
                "required": ["file", "directory", "link", "other"],
                "additionalProperties": False,
            },
            "total": {"type": "integer", "minimum": 0, "maximum": 200},
        },
        "required": ["counts", "total"],
        "additionalProperties": False,
    }
    output["required"] = ["system", "directory", "stats"]
    return {
        "kind": "skill",
        "manifest": {
            "kind": "skill",
            "capability_id": "user.workspace_overview",
            "description": "Read system information, list the workspace, then count the returned entries.",
            "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            "output_schema": output,
            "verification_profile": "skill_steps_v1",
            "dependencies": [],
        },
        "markdown": (
            "# 工作区概览与条目统计\n\n"
            "1. 读取系统信息。\n2. 列出 workspace 的直接条目（仍需目录审批）。\n"
            "3. 将前一步**已验证并返回**的条目交给固定版本的统计 Tool，不自行读取文件。\n\n"
            "如果目录结果 truncated=true，统计只代表返回的条目，不是整个目录总数。"
        ),
        "steps": [
            {"id": "system", "tool": "system.info", "tool_version": 1, "arguments": {}},
            {"id": "directory", "tool": "filesystem.list", "tool_version": 1,
             "arguments": {"root_id": "workspace", "limit": 100}},
            {
                "id": "stats",
                "tool_version_id": str(tool_version_id),
                "arguments": {"entries": {"$ref": {"step": "directory", "field": "entries"}}},
            },
        ],
        "tests": [{"name": "three-step composition", "expected_steps": ["system", "directory", "stats"]}],
    }


def fixture_generation(kind: str, need: str, *, parent_version_id: UUID | None,
                       tool_version_id: UUID | None) -> str:
    if kind == "tool":
        value = {
            "kind": "tool",
            "manifest": TOOL_MANIFEST,
            "source": TOOL_SOURCE,
            "tests": [
                {"name": "mixed", "input": {"entries": [
                    {"name": "a", "kind": "file"}, {"name": "b", "kind": "file"},
                    {"name": "docs", "kind": "directory"}, {"name": "shortcut", "kind": "link"},
                    {"name": "device", "kind": "other"}]},
                 "expect_output": {"counts": {"file": 2, "directory": 1, "link": 1, "other": 1}, "total": 5}},
                {"name": "empty", "input": {"entries": []},
                 "expect_output": {"counts": {"file": 0, "directory": 0, "link": 0, "other": 0}, "total": 0}},
            ],
        }
    elif parent_version_id is None:
        value = _skill_v1()
    elif tool_version_id is not None:
        value = _skill_v2(tool_version_id)
    else:
        raise ValueError("the composition fixture requires an existing Tool candidate")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
