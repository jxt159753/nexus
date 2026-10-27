"""Bound a result preview to verified evidence, current policy and the native call."""

import hashlib
import json
from datetime import timedelta
from uuid import UUID

from nexus.contracts.tools import validate_result
from nexus.core.cognitive.chat_context import pressure
from nexus.core.models.chat_format import parse_proposal
from nexus.core.security.service import scope_digest
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict, canonical, load_task

RESULT_FIELDS = {
    "get_system_info": ("os_name", "os_version", "architecture", "python_version"),
    "list_directory": ("root_id", "entries", "truncated"),
}


async def queue_summary(conn, row, consented_at: str) -> None:
    await conn.execute("UPDATE chat_turns SET phase='summary',initial_response_text=response_text,response_text='',status='queued',error_code=NULL,summary_consented_at=?,auto_result_status=CASE WHEN auto_result_json IS NULL THEN 'none' ELSE 'queued' END,revision=revision+1 WHERE turn_id=?", (consented_at, row["turn_id"]))
    await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id=?", (row["conversation_id"],))


def checked_native(value: dict, proposal: dict) -> dict:
    if (set(value) != {"id", "type", "function"} or value["type"] != "function"
            or not isinstance(value["id"], str) or not 1 <= len(value["id"]) <= 128
            or set(value["function"]) != {"name", "arguments"}
            or parse_proposal(value["function"]["name"], value["function"]["arguments"]) != proposal):
        raise ValueError("native call does not match proposal")
    return value


async def result_source(chat, conn, row) -> tuple[dict, dict]:
    if row["skill_run_id"]:
        from nexus.core.cognitive.chat_skills import result_source as skill_result_source
        if not row["native_tool_call_json"]:
            raise AccessDenied("RESULT_UNAVAILABLE")
        return await skill_result_source(chat, conn, row)
    if not row["task_id"] or not row["native_tool_call_json"] or row["action_status"] != "submitted":
        raise AccessDenied("RESULT_UNAVAILABLE")
    task = await load_task(conn, UUID(row["task_id"]))
    proposal = json.loads(row["proposal_json"])
    tool = {"get_system_info": "system.info", "list_directory": "filesystem.list"}[proposal["intent"]]
    if (task.owner_id != UUID(row["owner_id"]) or task.node_id != UUID(json.loads(row["request_json"])["node_id"])
            or task.status != "succeeded" or task.execution_outcome != "known" or task.result is None
            or task.verification != "node_report_schema_checked" or task.request.request_id != UUID(row["turn_id"])
            or task.action.tool != tool or task.action.tool_version != 1
            or task.action.arguments.model_dump(mode="json") != proposal["arguments"]):
        raise AccessDenied("RESULT_UNAVAILABLE")
    scope = chat.harness.scope(task.node_id)
    version = await chat.harness.security._policy_eligible(conn, task, scope, utc_now(), require_unexpired=False)
    result = validate_result(task.action, task.result, 32768).model_dump(mode="json")
    return result, {"task_id": str(task.task_id), "task_revision": task.revision,
                    "policy_version": version, "scope_digest": scope_digest(scope),
                    "source_digest": hashlib.sha256(canonical(result).encode()).hexdigest()}


async def preview(chat, conn, row, conversation, fields) -> dict:
    from nexus.core.memory.context import validate_turn
    await validate_turn(conn, row)
    result, binding = await result_source(chat, conn, row)
    selected = sorted(set(fields if fields is not None else result))
    if not selected or not set(selected) <= result.keys():
        raise AccessDenied("POLICY_DENIED")
    # Metadata stays explicit even when users omit the entries or OS fields.
    skill = bool(row["skill_run_id"])
    truncated = bool(result.get("truncated", False)) or (skill and any(value.get("truncated", False) for value in result.values()))
    selected_result = json.loads(canonical({key: result[key] for key in selected}))
    if "entries" in selected_result:
        selected_result["entries"] = list(selected_result["entries"])
        while len(canonical(selected_result).encode()) > 8192:
            selected_result["entries"].pop()
            truncated = True
        if "truncated" in selected_result:
            selected_result["truncated"] = truncated
    data = {"result": selected_result, "omitted_fields": sorted(result.keys()-set(selected)),
            "truncated": truncated, "verification": "skill_steps_verified" if skill else "node_report_schema_checked"}
    if skill:
        data["confirmed_input"] = json.loads(row["skill_plan_json"])["input"]
        data["counts_scope"] = "returned_entries_only"
        while len(canonical(data).encode()) > 8192:
            directory = next((value for value in selected_result.values() if value.get("entries")), None)
            if directory is None:
                raise AccessDenied("MODEL_INPUT_LIMIT")
            directory["entries"].pop()
            directory["truncated"] = True
            data["truncated"] = True
    native = checked_native(json.loads(row["native_tool_call_json"]), json.loads(row["proposal_json"]))
    messages = [*json.loads(row["context_json"]),
                {"role": "assistant", "content": row["response_text"] or None, "tool_calls": [native]},
                {"role": "tool", "tool_call_id": native["id"], "content": canonical(data)}]
    # Never silently change the originally consented conversation context.
    if not pressure(chat.config, messages, final_result=True, assistant_style=json.loads(row['assistant_style_json']))["fits"]:
        raise AccessDenied("MODEL_INPUT_LIMIT")
    return {**binding, "available_fields": sorted(result), "fields": selected, "data": data,
            "messages": messages, "conversation_revision": conversation["revision"],
            "expires_at": timestamp(utc_now()+timedelta(minutes=5))}


async def validate_preview(chat, conn, row) -> dict:
    value = json.loads(row["result_preview_json"])
    if value["expires_at"] <= timestamp(utc_now()):
        raise AccessDenied("APPROVAL_EXPIRED")
    _, binding = await result_source(chat, conn, row)
    if any(value[key] != current for key, current in binding.items()):
        raise AccessDenied("POLICY_DENIED")
    if await one(conn, "SELECT 1 FROM chat_turns WHERE conversation_id=? AND rowid>(SELECT rowid FROM chat_turns WHERE turn_id=?)", (row["conversation_id"], row["turn_id"])):
        raise Conflict("IDEMPOTENCY_CONFLICT")
    return value


def public_preview(value: dict, digest: str) -> dict:
    return {key: value[key] for key in ("fields", "available_fields", "data", "expires_at")} | {
        "result_digest": digest, "expected_revision": value["conversation_revision"]}
