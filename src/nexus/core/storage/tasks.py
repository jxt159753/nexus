"""Task authority and bounded owner history; execution evidence uses attempts."""

import json
import time
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import aiosqlite
from pydantic import TypeAdapter

from nexus.contracts.base import StrictContract, UTCDateTime
from nexus.contracts.errors import ErrorPayload
from nexus.contracts.task import ExecutionOutcome, TaskRequest, TaskStatus
from nexus.contracts.tools import ToolCall, ToolResult
from nexus.core.cognitive.router import route_task
from nexus.core.storage.database import Database, audit, one, timestamp, utc_now

REQUEST = TypeAdapter(TaskRequest)
ACTION = TypeAdapter(ToolCall)
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}
TRANSITIONS = {
    "queued": {"awaiting_approval", "ready", "failed", "cancelled", "timed_out"},
    "awaiting_approval": {"ready", "failed", "cancelled", "timed_out"},
    "ready": {"awaiting_approval", "failed", "cancelled", "timed_out"},
}
EXECUTION_TRANSITIONS = {
    "ready": {"dispatching"},
    "dispatching": {"running", "verifying", "recovering", "cancelled", "timed_out", "failed"},
    "running": {"verifying", "recovering", "cancelled", "timed_out", "failed"},
    "recovering": {"ready", "awaiting_approval", "running", "verifying", "cancelled", "timed_out", "failed"},
    "verifying": {"succeeded", "failed", "recovering", "timed_out"},
}


class Conflict(ValueError):
    pass


class TaskRecord(StrictContract):
    task_id: UUID
    owner_id: UUID
    node_id: UUID
    request: TaskRequest
    action: ToolCall
    status: TaskStatus
    execution_outcome: ExecutionOutcome
    revision: int
    created_at: UTCDateTime
    expires_at: UTCDateTime
    current_attempt_id: UUID | None = None
    result: ToolResult | None = None
    error: ErrorPayload | None = None
    verification: str | None = None
    cancel_requested: bool = False
    project_id: UUID | None = None


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def task_record(row: aiosqlite.Row) -> TaskRecord:
    return TaskRecord(
        task_id=UUID(row["task_id"]), owner_id=UUID(row["owner_id"]), node_id=UUID(row["node_id"]),
        request=REQUEST.validate_json(row["request_json"]), action=ACTION.validate_json(row["action_json"]),
        status=row["status"], execution_outcome=row["execution_outcome"], revision=row["revision"],
        created_at=row["created_at"], expires_at=row["expires_at"],
        current_attempt_id=UUID(row["current_attempt_id"]) if row["current_attempt_id"] else None,
        result=TypeAdapter(ToolResult).validate_json(row["result_json"]) if row["result_json"] else None,
        error=ErrorPayload.model_validate_json(row["error_json"]) if row["error_json"] else None,
        verification=row["verification"],
        cancel_requested=bool(row["cancel_requested"]),
        project_id=UUID(row["project_id"]) if row["project_id"] else None,
    )


async def load_task(conn: aiosqlite.Connection, task_id: UUID) -> TaskRecord:
    row = await one(conn, "SELECT * FROM tasks WHERE task_id=?", (str(task_id),))
    if row is None:
        raise LookupError("task not found")
    return task_record(row)


async def transition(
    conn: aiosqlite.Connection, task: TaskRecord, status: TaskStatus,
    actor_id: UUID, now: datetime, *, execution: bool = False,
) -> TaskRecord:
    allowed = EXECUTION_TRANSITIONS if execution else TRANSITIONS
    if status not in allowed.get(task.status, set()):
        raise Conflict("illegal task transition")
    if now >= task.expires_at and status not in {"timed_out", "cancelled"} and not execution:
        raise Conflict("task expired")
    async with conn.execute(
        "UPDATE tasks SET status=?, revision=revision+1 WHERE task_id=? AND revision=?",
        (status, str(task.task_id), task.revision),
    ) as cursor:
        if cursor.rowcount != 1:
            raise Conflict("stale task revision")
    await conn.execute("INSERT INTO task_events(task_id, revision, kind, created_at) VALUES (?, ?, ?, ?)", (
        str(task.task_id), task.revision + 1, status, timestamp(now),
    ))
    await audit(conn, str(actor_id), str(task.task_id), f"task.{status}", now)
    return await load_task(conn, task.task_id)


class TaskRepository:
    def __init__(self, database: Database):
        self.database = database

    async def create(
        self, owner_id: UUID, request: TaskRequest, node_id: UUID, *, now: datetime | None = None,
        rate_limit: bool = False,
        language_id: UUID | None = None,
        chat_turn_id: UUID | None = None,
        skill_run_id: UUID | None = None,
        skill_step_index: int | None = None,
        work_step_id: UUID | None = None,
        work_role: str = "execute",
    ) -> TaskRecord:
        now = now or utc_now()
        request = REQUEST.validate_json(REQUEST.dump_json(request))
        started = time.perf_counter()
        routing = route_task(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        action = ACTION.validate_json(canonical({
            "tool": routing.tool_name, "tool_version": routing.tool_version, "arguments": routing.arguments,
        }))
        encoded = canonical(request.model_dump(mode="json"))
        async with self.database.transaction() as conn:
            if work_step_id is not None:
                from nexus.core.cognitive.chat_work import validate_task
                await validate_task(conn, work_step_id, work_role, owner_id, node_id, request, now)
            if skill_run_id is not None:
                from nexus.core.extensions.authorization import skill_run_allowed
                run = await one(conn, "SELECT * FROM skill_runs WHERE run_id=? AND owner_id=? AND node_id=?", (str(skill_run_id), str(owner_id), str(node_id)))
                if not await skill_run_allowed(conn, run, now) or run["current_step"] != skill_step_index:
                    raise Conflict("Skill execution authorization changed")
            for identity, role in ((owner_id, "owner"), (node_id, "node")):
                row = await one(conn, "SELECT role, revoked_at FROM identities WHERE identity_id=?", (str(identity),))
                if row is None or row["role"] != role or row["revoked_at"] is not None:
                    raise PermissionError("inactive or incorrect identity role")
            existing = await one(conn, "SELECT * FROM tasks WHERE owner_id=? AND request_id=?", (str(owner_id), str(request.request_id)))
            if existing:
                if canonical(REQUEST.validate_json(existing["request_json"]).model_dump(mode="json")) != encoded or existing["node_id"] != str(node_id):
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                if skill_run_id is not None and not await one(conn, "SELECT 1 FROM skill_run_steps WHERE run_id=? AND step_index=? AND task_id=?", (str(skill_run_id), skill_step_index, existing["task_id"])):
                    raise Conflict("Skill task association changed")
                if work_step_id is not None and not await one(conn,"SELECT 1 FROM chat_work_tasks WHERE task_id=? AND step_id=? AND role=?",(existing["task_id"],str(work_step_id),work_role)):
                    raise Conflict("work task association changed")
                return task_record(existing)
            if request.project_id is not None and not await one(conn, "SELECT 1 FROM projects WHERE project_id=? AND owner_id=?", (str(request.project_id), str(owner_id))):
                raise LookupError("project not found")
            if chat_turn_id is not None:
                chat=await one(conn,"SELECT * FROM chat_turns WHERE turn_id=? AND owner_id=?",(str(chat_turn_id),str(owner_id)))
                version=(await one(conn,"SELECT version FROM policy_state WHERE singleton=1"))[0]
                expected={"intent":request.intent,"arguments":request.arguments.model_dump(mode="json")}
                if chat is None or chat["status"]!="completed" or chat["action_status"]!="proposed" or chat["proposal_json"]!=canonical(expected) or json.loads(chat["request_json"]).get("node_id")!=str(node_id) or chat["proposal_policy_version"]!=version:
                    raise Conflict("chat proposal binding changed")
                conversation = await one(conn, "SELECT project_id FROM conversations WHERE conversation_id=?", (chat["conversation_id"],))
                if conversation["project_id"] != (str(request.project_id) if request.project_id else None):
                    raise Conflict("chat project binding changed")
                if chat["auto_result_json"] and await one(conn, "SELECT 1 FROM chat_turns WHERE conversation_id=? AND rowid>(SELECT rowid FROM chat_turns WHERE turn_id=?)", (chat["conversation_id"], str(chat_turn_id))):
                    raise Conflict("chat automatic result context changed")
            if request.intent == "write_file":
                from nexus.contracts.basic import text_digest
                preview = await one(conn, "SELECT * FROM tasks WHERE task_id=? AND owner_id=? AND node_id=?", (str(request.arguments.preview_task_id), str(owner_id), str(node_id)))
                if preview is None or preview["status"] != "succeeded" or preview["execution_outcome"] != "known" or not preview["verification"] or not preview["result_json"]:
                    raise Conflict("WRITE_PREVIEW_REQUIRED")
                data = json.loads(preview["result_json"])
                if (json.loads(preview["action_json"])["tool"] != "filesystem.write_preview" or preview["project_id"] != (str(request.project_id) if request.project_id else None)
                        or data["root_id"] != request.arguments.root_id or data["path"] != request.arguments.path
                        or data["content_sha256"] != text_digest(request.arguments.content) or data["previous_sha256"] != request.arguments.expected_sha256
                        or data["preview_digest"] != request.arguments.preview_digest):
                    raise Conflict("WRITE_PREVIEW_REQUIRED")
            if rate_limit:
                recent = await one(conn, "SELECT count(*) FROM tasks WHERE owner_id=? AND created_at>?", (str(owner_id), timestamp(now - timedelta(minutes=1))))
                if recent[0] >= 10:
                    raise Conflict("BUSY")
            count = await one(conn, "SELECT count(*) AS n FROM tasks WHERE status NOT IN ('succeeded','failed','cancelled','timed_out')")
            if count["n"] >= 100:
                raise Conflict("BUSY")
            task_id = uuid4()
            await conn.execute(
                "INSERT INTO tasks(task_id, owner_id, request_id, request_json, node_id, action_json, status, created_at, expires_at, project_id) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
                (str(task_id), str(owner_id), str(request.request_id), encoded, str(node_id), canonical(action.model_dump(mode="json")), timestamp(now), timestamp(now + timedelta(minutes=5)), str(request.project_id) if request.project_id else None),
            )
            await conn.execute("INSERT INTO routing_decisions VALUES(?,?,?,?,?,?,?,?, 'live')", (
                str(task_id), routing.mode, routing.reason_code, routing.policy_version, routing.tool_name,
                routing.tool_version, elapsed_ms, timestamp(now),
            ))
            await conn.execute("INSERT INTO task_events(task_id, revision, kind, created_at) VALUES (?, 0, 'queued', ?)", (str(task_id), timestamp(now)))
            if language_id is not None:
                linked = await conn.execute("UPDATE language_requests SET status='submitted',task_id=? WHERE language_id=? AND owner_id=? AND node_id=? AND status='proposed' AND task_id IS NULL AND expires_at>?", (
                    str(task_id), str(language_id), str(owner_id), str(node_id), timestamp(now)))
                if linked.rowcount != 1:
                    raise Conflict("language proposal no longer valid")
                await conn.execute("UPDATE model_calls SET task_id=? WHERE language_id=?", (str(task_id), str(language_id)))
                await audit(conn, str(owner_id), str(task_id), "language.confirmed", now)
            if chat_turn_id is not None:
                linked=await conn.execute("UPDATE chat_turns SET task_id=?,action_status='submitted',auto_result_status=CASE WHEN auto_result_json IS NULL THEN 'none' ELSE 'waiting' END,revision=revision+1 WHERE turn_id=? AND owner_id=? AND action_status='proposed' AND task_id IS NULL AND expires_at>?",(str(task_id),str(chat_turn_id),str(owner_id),timestamp(now)))
                if linked.rowcount!=1:raise Conflict("chat proposal no longer valid")
                await conn.execute("UPDATE model_calls SET task_id=? WHERE chat_turn_id=?",(str(task_id),str(chat_turn_id)))
                await audit(conn,str(owner_id),str(task_id),"chat.action_confirmed",now)
            await audit(conn, str(owner_id), str(task_id), "task.created", now)
            if work_step_id is not None:
                from nexus.core.security.work import operation
                await operation(conn, work_step_id, "task:"+str(task_id), now)
                await conn.execute("INSERT INTO chat_work_tasks VALUES(?,?,?)", (str(task_id), str(work_step_id), work_role))
                field = "preview_task_id" if work_role == "preview" else "task_id"
                await conn.execute(f"UPDATE chat_work_steps SET {field}=?,status='waiting' WHERE step_id=?", (str(task_id), str(work_step_id)))
            elif skill_run_id is not None:
                from nexus.core.security.work import operation
                work = await one(conn,"SELECT step_id FROM chat_work_steps WHERE skill_run_id=?",(str(skill_run_id),))
                if work:
                    await operation(conn, work[0], "task:"+str(task_id), now)
            if skill_run_id is not None:
                linked = await conn.execute("UPDATE skill_run_steps SET task_id=?,status='dispatchable',updated_at=? WHERE run_id=? AND step_index=? AND task_id IS NULL AND status='pending'", (str(task_id), timestamp(now), str(skill_run_id), skill_step_index))
                if linked.rowcount != 1:
                    raise Conflict("Skill step association changed")
            return await load_task(conn, task_id)

    async def get(self, task_id: UUID, owner_id: UUID) -> TaskRecord:
        async with self.database.transaction(write=False) as conn:
            task = await load_task(conn, task_id)
            if task.owner_id != owner_id:
                raise PermissionError("task owner mismatch")
            return task

    async def history(self, owner_id: UUID, *, after_event_id: int = 0, limit: int = 100) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 200 or type(after_event_id) is not int or after_event_id < 0:
            raise ValueError("invalid history bounds")
        async with self.database.transaction(write=False) as conn:
            rows = await conn.execute_fetchall(
                "SELECT e.* FROM task_events e JOIN tasks t USING(task_id) WHERE t.owner_id=? AND event_id>? ORDER BY event_id LIMIT ?",
                (str(owner_id), after_event_id, limit),
            )
            return [dict(row) for row in rows]

    async def list(self, owner_id: UUID, *, after: int = 0, limit: int = 20) -> tuple[list[TaskRecord], int]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid task page")
        async with self.database.transaction(write=False) as conn:
            rows = await conn.execute_fetchall("SELECT rowid AS cursor, * FROM tasks WHERE owner_id=? AND rowid>? ORDER BY rowid LIMIT ?", (str(owner_id), after, limit))
            return [task_record(row) for row in rows], rows[-1]["cursor"] if rows else after

    async def transition(self, task_id: UUID, owner_id: UUID, expected_revision: int, status: TaskStatus) -> TaskRecord:
        """Trusted Core persistence primitive; never expose directly to clients."""
        async with self.database.transaction() as conn:
            task = await load_task(conn, task_id)
            if task.owner_id != owner_id:
                raise PermissionError("task owner mismatch")
            if task.revision != expected_revision:
                raise Conflict("stale task revision")
            return await transition(conn, task, status, owner_id, utc_now())
