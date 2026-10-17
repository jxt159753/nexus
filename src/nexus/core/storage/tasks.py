"""Predispatch task authority. Execution/attempt evidence belongs to P07."""

import json
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import aiosqlite
from pydantic import TypeAdapter

from nexus.contracts.base import StrictContract, UTCDateTime
from nexus.contracts.task import ExecutionOutcome, TaskRequest, TaskStatus
from nexus.contracts.tools import ToolCall
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


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def task_record(row: aiosqlite.Row) -> TaskRecord:
    return TaskRecord(
        task_id=UUID(row["task_id"]), owner_id=UUID(row["owner_id"]), node_id=UUID(row["node_id"]),
        request=REQUEST.validate_json(row["request_json"]), action=ACTION.validate_json(row["action_json"]),
        status=row["status"], execution_outcome=row["execution_outcome"], revision=row["revision"],
        created_at=row["created_at"], expires_at=row["expires_at"],
    )


async def load_task(conn: aiosqlite.Connection, task_id: UUID) -> TaskRecord:
    row = await one(conn, "SELECT * FROM tasks WHERE task_id=?", (str(task_id),))
    if row is None:
        raise LookupError("task not found")
    return task_record(row)


async def transition(
    conn: aiosqlite.Connection, task: TaskRecord, status: TaskStatus,
    actor_id: UUID, now: datetime,
) -> TaskRecord:
    if status not in TRANSITIONS.get(task.status, set()):
        raise Conflict("illegal task transition")
    if now >= task.expires_at and status not in {"timed_out", "cancelled"}:
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
    ) -> TaskRecord:
        now = now or utc_now()
        request = REQUEST.validate_json(REQUEST.dump_json(request))
        routing = route_task(request)
        action = ACTION.validate_json(canonical({
            "tool": routing.tool_name, "tool_version": routing.tool_version, "arguments": routing.arguments,
        }))
        encoded = canonical(request.model_dump(mode="json"))
        async with self.database.transaction() as conn:
            for identity, role in ((owner_id, "owner"), (node_id, "node")):
                row = await one(conn, "SELECT role, revoked_at FROM identities WHERE identity_id=?", (str(identity),))
                if row is None or row["role"] != role or row["revoked_at"] is not None:
                    raise PermissionError("inactive or incorrect identity role")
            existing = await one(conn, "SELECT * FROM tasks WHERE owner_id=? AND request_id=?", (str(owner_id), str(request.request_id)))
            if existing:
                if existing["request_json"] != encoded or existing["node_id"] != str(node_id):
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return task_record(existing)
            count = await one(conn, "SELECT count(*) AS n FROM tasks WHERE status NOT IN ('succeeded','failed','cancelled','timed_out')")
            if count["n"] >= 100:
                raise Conflict("BUSY")
            task_id = uuid4()
            await conn.execute(
                "INSERT INTO tasks(task_id, owner_id, request_id, request_json, node_id, action_json, status, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                (str(task_id), str(owner_id), str(request.request_id), encoded, str(node_id), canonical(action.model_dump(mode="json")), timestamp(now), timestamp(now + timedelta(minutes=5))),
            )
            await conn.execute("INSERT INTO task_events(task_id, revision, kind, created_at) VALUES (?, 0, 'queued', ?)", (str(task_id), timestamp(now)))
            await audit(conn, str(owner_id), str(task_id), "task.created", now)
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

    async def transition(self, task_id: UUID, owner_id: UUID, expected_revision: int, status: TaskStatus) -> TaskRecord:
        """Trusted Core persistence primitive; never expose directly to clients."""
        async with self.database.transaction() as conn:
            task = await load_task(conn, task_id)
            if task.owner_id != owner_id:
                raise PermissionError("task owner mismatch")
            if task.revision != expected_revision:
                raise Conflict("stale task revision")
            return await transition(conn, task, status, owner_id, utc_now())
