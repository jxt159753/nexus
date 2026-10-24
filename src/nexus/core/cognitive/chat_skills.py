"""Finite Skill catalog, immutable plan confirmation and verified result sources.

This module does not execute steps. SkillService and Harness own execution.
"""

import json
from uuid import NAMESPACE_URL, UUID, uuid5

from nexus.contracts.extensions import RESULT_FIELDS, canonical, digest_value, validate_instance
from nexus.contracts.tools import validate_result
from nexus.core.extensions.skills import _resolve
from nexus.core.extensions.tools import reference_stats
from nexus.core.security.service import scope_digest
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict, load_task


async def eligible(skills, conn, owner_id: str, version, node_id: UUID) -> list[dict]:
    skills.extensions.require_enabled()
    if version is None or version["owner_id"] != owner_id or version["kind"] != "skill":
        raise AccessDenied("VERSION_NOT_APPROVED")
    now = utc_now()
    approval = await skills.extensions.current_approval(conn, version, now)
    if approval["node_id"] not in {None, str(node_id)}:
        raise AccessDenied("POLICY_DENIED")
    scope = skills.harness.scope(node_id)
    if not await one(conn, "SELECT 1 FROM identities WHERE identity_id=? AND role='node' AND revoked_at IS NULL", (str(node_id),)):
        raise AccessDenied("POLICY_DENIED")
    bindings = [{"version_id": version["version_id"], "artifact_digest": version["artifact_digest"],
                 "environment_digest": version["environment_digest"], "approval_id": approval["approval_id"]}]
    steps = json.loads(version["steps_json"])
    if not 1 <= len(steps) <= 3:
        raise AccessDenied("POLICY_DENIED")
    for step in steps:
        if step.get("tool_version_id"):
            tool = await one(conn, "SELECT * FROM extension_versions WHERE version_id=? AND kind='tool' AND owner_id=?", (step["tool_version_id"], owner_id))
            if tool is None:
                raise AccessDenied("DEPENDENCY_UNAVAILABLE")
            approved = await skills.extensions.current_approval(conn, tool, now)
            bindings.append({"version_id": tool["version_id"], "artifact_digest": tool["artifact_digest"],
                             "environment_digest": tool["environment_digest"], "approval_id": approved["approval_id"],
                             "node_id": approved["node_id"]})
        else:
            tool = step["tool"]
            root = "workspace" if tool == "filesystem.list" else ""
            key = f"{tool}@1"
            if key not in scope.tools or key not in scope.local_tools or (root and (root not in scope.roots or root not in scope.local_roots)):
                raise AccessDenied("POLICY_DENIED")
            for identity in (owner_id, str(node_id)):
                if not await one(conn, "SELECT 1 FROM grants WHERE identity_id=? AND tool=? AND tool_version=1 AND root_id=?", (identity, tool, root)):
                    raise AccessDenied("POLICY_DENIED")
    return bindings


async def catalog(chat, conn, principal, node_id: UUID) -> dict:
    await chat.owner(conn, principal)
    entries = []
    if chat.skills is not None and chat.skills.extensions.config.enabled:
        versions = await conn.execute_fetchall("SELECT * FROM extension_versions WHERE owner_id=? AND kind='skill' AND enabled=1 ORDER BY rowid DESC LIMIT 100", (str(principal.identity_id),))
        for version in versions:
            try:
                await eligible(chat.skills, conn, str(principal.identity_id), version, node_id)
            except AccessDenied:
                continue
            manifest = json.loads(version["manifest_json"])
            entry = {"version_id": version["version_id"], "capability_id": version["capability_id"],
                     "revision": version["revision"], "artifact_digest": version["artifact_digest"],
                     "description": manifest["description"], "input_schema": manifest["input_schema"],
                     "steps": [{"id": step["id"], "tool": step.get("tool", "approved_json_tool"),
                                "approval_required": step.get("tool") == "filesystem.list"}
                               for step in json.loads(version["steps_json"])]}
            if len(canonical([*entries, entry]).encode()) <= 8192:
                entries.append(entry)
            if len(entries) == 8:
                break
    # Also bind consent to the selected device without sending its identity.
    return {"skills": entries, "skills_digest": digest_value({"node_id": str(node_id), "skills": entries}),
            "limit": 8, "notice": "仅这些已启用固定版本可被建议；没有匹配流程时说明能力缺口。"}


async def build_plan(skills, conn, row, proposal: dict, input_value: dict, *, require_valid: bool = False) -> dict:
    version_id = proposal["arguments"]["version_id"]
    snapshot = next((item for item in json.loads(row["skill_catalog_json"]) if item["version_id"] == version_id), None)
    if snapshot is None or not json.loads(row["request_json"]).get("allow_skills"):
        raise AccessDenied("POLICY_DENIED")
    version = await one(conn, "SELECT * FROM extension_versions WHERE version_id=?", (version_id,))
    if version is None or version["artifact_digest"] != snapshot["artifact_digest"]:
        raise AccessDenied("VERSION_NOT_APPROVED")
    node_id = UUID(json.loads(row["request_json"])["node_id"])
    bindings = await eligible(skills, conn, row["owner_id"], version, node_id)
    manifest = json.loads(version["manifest_json"])
    ready = True
    try:
        validate_instance(manifest["input_schema"], input_value)
        for step in json.loads(version["steps_json"]):
            independent = {key: value for key, value in step.get("arguments", {}).items()
                           if not (isinstance(value, dict) and "$ref" in value)}
            resolved = _resolve(independent, {}, input_value)
            if step.get("tool") == "filesystem.list":
                from nexus.contracts.task import ListDirectoryArguments
                ListDirectoryArguments.model_validate(resolved)
            elif step.get("tool_version_id"):
                dependency = await one(conn, "SELECT manifest_json FROM extension_versions WHERE version_id=?", (step["tool_version_id"],))
                properties = json.loads(dependency[0])["input_schema"]["properties"]
                for key, value in resolved.items():
                    validate_instance(properties[key], value)
    except (ValueError, KeyError):
        if require_valid:
            raise ValueError("请按流程输入格式补齐或修改参数") from None
        ready = False
    steps = []
    dependency_nodes = {binding["version_id"]: binding.get("node_id") for binding in bindings[1:]}
    for index, step in enumerate(json.loads(version["steps_json"]), start=1):
        arguments = {}
        for key, value in step.get("arguments", {}).items():
            arguments[key] = input_value.get(value["$input"]["field"], value) if isinstance(value, dict) and "$input" in value else value
        steps.append({"index": index, "id": step["id"], "tool": step.get("tool", "approved_json_tool"),
                      "tool_version_id": step.get("tool_version_id"), "tool_version": step.get("tool_version"),
                      "arguments": arguments, "node_id": dependency_nodes[step["tool_version_id"]] if step.get("tool_version_id") else str(node_id),
                      "approval_required": step.get("tool") == "filesystem.list"})
    return {"turn_id": row["turn_id"], "version_id": version_id, "capability_id": version["capability_id"],
            "revision": version["revision"], "artifact_digest": version["artifact_digest"], "bindings": bindings,
            "input": input_value, "input_schema": manifest["input_schema"], "steps": steps, "ready": ready,
            "node_id": str(node_id), "policy_version": (await one(conn, "SELECT version FROM policy_state WHERE singleton=1"))[0],
            "scope_digest": scope_digest(skills.harness.scope(node_id)), "expires_at": row["expires_at"],
            "result_policy": "成功后选择步骤，预览白名单结果再同意外发；最多追加一次，沿用本轮 USD 0.05。"}


async def validate_confirmation(skills, conn, principal, turn_id, digest, version_id, node_id, input_value):
    row = await one(conn, "SELECT t.*,c.archived FROM chat_turns t JOIN conversations c USING(conversation_id) WHERE turn_id=? AND t.owner_id=?", (str(turn_id), str(principal.identity_id)))
    if row is None:
        raise LookupError("chat turn not found")
    if not digest or row["proposal_digest"] != digest or not row["skill_plan_json"]:
        raise Conflict("IDEMPOTENCY_CONFLICT")
    plan = json.loads(row["skill_plan_json"])
    if (plan["version_id"] != str(version_id) or plan["node_id"] != str(node_id) or plan["input"] != input_value
            or digest_value(plan) != digest):
        raise Conflict("IDEMPOTENCY_CONFLICT")
    if row["skill_run_id"] and row["action_status"] == "submitted":
        return row
    if row["status"] != "completed" or row["phase"] != "reply" or row["action_status"] != "proposed" or row["archived"] or row["expires_at"] <= timestamp(utc_now()):
        raise AccessDenied("APPROVAL_EXPIRED")
    if skills.failed or skills.harness.failed or (await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0]:
        raise AccessDenied("BUSY")
    if await one(conn, "SELECT 1 FROM chat_turns WHERE conversation_id=? AND rowid>(SELECT rowid FROM chat_turns WHERE turn_id=?)", (row["conversation_id"], str(turn_id))):
        raise Conflict("IDEMPOTENCY_CONFLICT")
    current = await build_plan(skills, conn, row, json.loads(row["proposal_json"]), input_value, require_valid=True)
    if current != plan:
        raise AccessDenied("POLICY_DENIED")
    return row


async def result_source(chat, conn, row) -> tuple[dict, dict]:
    if chat.skills is None or not row["skill_run_id"] or row["action_status"] != "submitted":
        raise AccessDenied("RESULT_UNAVAILABLE")
    run = await one(conn, "SELECT * FROM skill_runs WHERE run_id=? AND owner_id=?", (row["skill_run_id"], row["owner_id"]))
    plan = json.loads(row["skill_plan_json"])
    if (run is None or run["status"] != "succeeded" or run["version_id"] != plan["version_id"]
            or run["artifact_digest"] != plan["artifact_digest"] or run["node_id"] != plan["node_id"]
            or json.loads(run["input_json"]) != plan["input"] or run["request_id"] != row["turn_id"]):
        raise AccessDenied("RESULT_UNAVAILABLE")
    version = await one(conn, "SELECT * FROM extension_versions WHERE version_id=?", (run["version_id"],))
    bindings = await eligible(chat.skills, conn, row["owner_id"], version, UUID(run["node_id"]))
    if bindings != plan["bindings"]:
        raise AccessDenied("POLICY_DENIED")
    result = json.loads(run["result_json"])
    validate_instance(json.loads(version["manifest_json"])["output_schema"], result)
    steps = await conn.execute_fetchall("SELECT * FROM skill_run_steps WHERE run_id=? ORDER BY step_index", (run["run_id"],))
    safe = {}
    if len(steps) != len(plan["steps"]):
        raise AccessDenied("RESULT_UNAVAILABLE")
    for step, definition in zip(steps, plan["steps"]):
        if step["status"] != "succeeded" or step["step_id"] != definition["id"]:
            raise AccessDenied("RESULT_UNAVAILABLE")
        evidence = json.loads(step["result_json"])
        if evidence != result.get(step["step_id"]):
            raise AccessDenied("RESULT_UNAVAILABLE")
        arguments = _resolve(next(item for item in json.loads(version["steps_json"]) if item["id"] == step["step_id"])["arguments"], result, plan["input"])
        if step["task_id"]:
            task = await load_task(conn, UUID(step["task_id"]))
            expected_request = uuid5(NAMESPACE_URL, f"nexus-skill:{run['run_id']}:{step['step_index']}")
            if (task.status != "succeeded" or task.execution_outcome != "known" or task.verification != "node_report_schema_checked"
                    or task.result is None or task.result.model_dump(mode="json") != evidence or task.owner_id != UUID(row["owner_id"]) or task.node_id != UUID(run["node_id"])
                    or task.request.request_id != expected_request or task.action.tool != definition["tool"]
                    or task.action.tool_version != 1 or task.action.arguments.model_dump(mode="json") != arguments):
                raise AccessDenied("RESULT_UNAVAILABLE")
            await chat.harness.security._policy_eligible(conn, task, chat.harness.scope(task.node_id), utc_now(), require_unexpired=False)
            validate_result(task.action, task.result, 32768)
            safe[step["step_id"]] = {key: evidence[key] for key in RESULT_FIELDS[definition["tool"]]}
        elif step["tool_run_id"]:
            tool = await one(conn, "SELECT * FROM extension_tool_runs WHERE tool_run_id=?", (step["tool_run_id"],))
            if tool is None or tool["status"] != "succeeded" or json.loads(tool["output_json"]) != evidence or tool["skill_run_id"] != run["run_id"] or tool["version_id"] != definition["tool_version_id"]:
                raise AccessDenied("RESULT_UNAVAILABLE")
            if json.loads(tool["input_json"]) != arguments or evidence != reference_stats(arguments["entries"]):
                raise AccessDenied("RESULT_UNAVAILABLE")
            safe[step["step_id"]] = {key: evidence[key] for key in ("counts", "total")}
        else:
            raise AccessDenied("RESULT_UNAVAILABLE")
    return safe, {"skill_run_id": run["run_id"], "skill_run_revision": run["revision"],
                  "policy_version": (await one(conn, "SELECT version FROM policy_state WHERE singleton=1"))[0],
                  "scope_digest": scope_digest(chat.harness.scope(UUID(run["node_id"]))),
                  "source_digest": digest_value(result), "plan_digest": row["proposal_digest"]}
