"""Persistent owner controls, cancellation intent and task deadline authority."""

import json
from datetime import datetime
from uuid import UUID

import aiosqlite

from nexus.contracts.errors import ErrorCode, create_error
from nexus.core.security.service import assert_owner
from nexus.core.security.types import Principal
from nexus.core.storage.database import Database, audit, one, timestamp, utc_now
from nexus.core.storage.tasks import TERMINAL, Conflict, TaskRecord, load_task, transition


async def task_event(conn: aiosqlite.Connection, task: TaskRecord, kind: str, actor: UUID, now: datetime) -> None:
    cursor = await conn.execute("UPDATE tasks SET revision=revision+1 WHERE task_id=? AND revision=?", (str(task.task_id), task.revision))
    if cursor.rowcount != 1:
        raise Conflict("stale task revision")
    await conn.execute("INSERT INTO task_events(task_id,revision,kind,created_at) VALUES(?,?,?,?)", (str(task.task_id), task.revision + 1, kind, timestamp(now)))
    await audit(conn, str(actor), str(task.task_id), kind, now)


async def cancel_task(conn: aiosqlite.Connection, task: TaskRecord, actor: UUID, now: datetime) -> str:
    if task.status in TERMINAL or task.status == "verifying":
        return "already_finished"
    if task.cancel_requested:
        return "requested"
    await conn.execute("UPDATE tasks SET cancel_requested=1 WHERE task_id=?", (str(task.task_id),))
    active = await one(conn, "SELECT * FROM task_attempts WHERE attempt_id=?", (str(task.current_attempt_id),))
    if active is None or active["execution_stopped"]:
        await transition(conn, task, "cancelled", actor, now, execution=task.status == "recovering")
        return "cancelled"
    await conn.execute("UPDATE task_attempts SET stop_reason='cancel' WHERE attempt_id=?", (str(task.current_attempt_id),))
    await task_event(conn, task, "task.cancel_requested", actor, now)
    return "requested"


async def expire_task(conn: aiosqlite.Connection, task: TaskRecord, now: datetime) -> TaskRecord:
    if task.status in TERMINAL or now < task.expires_at:
        return task
    await conn.execute("UPDATE tasks SET error_json=? WHERE task_id=?", (create_error(ErrorCode.TOOL_TIMEOUT, "Task deadline exceeded").model_dump_json(), str(task.task_id)))
    if task.current_attempt_id:
        evidence = await conn.execute("UPDATE task_attempts SET phase='completed',execution_stopped=1 WHERE attempt_id=? AND result_json IS NOT NULL", (str(task.current_attempt_id),))
        if evidence.rowcount:
            await conn.execute("UPDATE tasks SET execution_outcome='known' WHERE task_id=?", (str(task.task_id),))
        else:
            await conn.execute("UPDATE task_attempts SET stop_reason='timeout' WHERE attempt_id=? AND phase!='completed'", (str(task.current_attempt_id),))
    return await transition(conn, task, "timed_out", task.owner_id, now, execution=task.status in {"dispatching", "running", "recovering", "verifying"})


class ControlRepository:
    def __init__(self, database: Database):
        self.database = database

    async def state(self) -> dict:
        async with self.database.transaction(write=False) as conn:
            row = await one(conn, "SELECT * FROM system_state WHERE singleton=1")
            event = await one(conn, "SELECT coalesce(max(event_id),0) FROM control_events")
            return {"paused": bool(row["paused"]), "revision": row["revision"], "event_id": str(event[0])}

    async def cancel(self, principal: Principal, task_id: UUID) -> dict:
        async with self.database.transaction() as conn:
            now = utc_now()
            await assert_owner(conn, principal, now)
            task = await load_task(conn, task_id)
            if task.owner_id != principal.identity_id:
                raise PermissionError("task owner mismatch")
            task = await expire_task(conn, task, now)
            disposition = await cancel_task(conn, task, principal.identity_id, now)
            return {"task": await load_task(conn, task_id), "disposition": disposition}

    async def apply(self, principal: Principal, kind: str, request_id: UUID) -> dict:
        if kind not in {"pause", "resume", "cancel_all"}:
            raise ValueError("unsupported control")
        async with self.database.transaction() as conn:
            now = utc_now()
            await assert_owner(conn, principal, now)
            old = await one(conn, "SELECT * FROM control_requests WHERE owner_id=? AND request_id=?", (str(principal.identity_id), str(request_id)))
            if old:
                if old["kind"] != kind:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return json.loads(old["response_json"])
            state = await one(conn, "SELECT * FROM system_state WHERE singleton=1")
            paused = bool(state["paused"]) if kind == "cancel_all" else kind == "pause"
            revision = state["revision"] + 1
            await conn.execute("UPDATE system_state SET paused=?, revision=? WHERE singleton=1", (int(paused), revision))
            event = await conn.execute("INSERT INTO control_events(owner_id,kind,paused,revision,created_at) VALUES(?,?,?,?,?)", (str(principal.identity_id), kind, int(paused), revision, timestamp(now)))
            affected = 0
            if kind == "cancel_all":
                rows = await conn.execute_fetchall("SELECT task_id FROM tasks WHERE owner_id=? AND status NOT IN ('succeeded','failed','cancelled','timed_out') LIMIT 100", (str(principal.identity_id),))
                for row in rows:
                    task = await expire_task(conn, await load_task(conn, UUID(row[0])), now)
                    if await cancel_task(conn, task, principal.identity_id, now) != "already_finished":
                        affected += 1
            await audit(conn, str(principal.identity_id), None, f"control.{kind}", now)
            response = {"paused": paused, "revision": revision, "event_id": str(event.lastrowid), "affected": affected}
            await conn.execute("INSERT INTO control_requests VALUES(?,?,?,?)", (str(principal.identity_id), str(request_id), kind, json.dumps(response)))
            return response

    async def expire(self) -> None:
        async with self.database.transaction() as conn:
            now = utc_now()
            rows = await conn.execute_fetchall("SELECT task_id FROM tasks WHERE expires_at<=? AND status NOT IN ('succeeded','failed','cancelled','timed_out') LIMIT 100", (timestamp(now),))
            for row in rows:
                await expire_task(conn, await load_task(conn, UUID(row[0])), now)
