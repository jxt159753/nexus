"""Project views and auditable, evidence-backed work items on the existing DB."""

import json
from uuid import uuid4

from nexus.core.memory.service import project_owned
from nexus.core.security.service import assert_owner
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict, canonical, task_record


class Projects:
    def __init__(self, database):
        self.database = database

    async def overview(self, principal, project_id, *, before=2**63-1):
        async with self.database.transaction(write=False) as conn:
            await assert_owner(conn, principal, utc_now())
            await project_owned(conn, project_id, principal.identity_id)
            project = await one(conn, "SELECT * FROM projects WHERE project_id=?", (str(project_id),))
            tasks = await conn.execute_fetchall("SELECT rowid AS cursor,* FROM tasks WHERE owner_id=? AND project_id=? AND rowid<? ORDER BY rowid DESC LIMIT 20", (str(principal.identity_id), str(project_id), before))
            conversations = await conn.execute_fetchall("SELECT rowid AS cursor,* FROM conversations WHERE owner_id=? AND project_id=? ORDER BY rowid DESC LIMIT 20", (str(principal.identity_id), str(project_id)))
            todos = await conn.execute_fetchall("SELECT * FROM project_todos WHERE owner_id=? AND project_id=? ORDER BY rowid DESC LIMIT 1000", (str(principal.identity_id), str(project_id)))
            counts = await conn.execute_fetchall("SELECT status,count(*) AS count FROM tasks WHERE owner_id=? AND project_id=? GROUP BY status", (str(principal.identity_id), str(project_id)))
            return {"project": dict(project), "tasks": [task_record(row).model_dump(mode="json") for row in tasks],
                    "next_before": tasks[-1]["cursor"] if tasks else None, "conversations": [dict(row) for row in conversations],
                    "todos": [dict(row) for row in todos], "task_counts": {row["status"]: row["count"] for row in counts}}

    async def create_todo(self, principal, project_id, value):
        async with self.database.transaction() as conn:
            now = utc_now()
            await assert_owner(conn, principal, now)
            await project_owned(conn, project_id, principal.identity_id)
            old = await one(conn, "SELECT * FROM project_todos WHERE owner_id=? AND request_id=?", (str(principal.identity_id), str(value.request_id)))
            if old:
                event = await one(conn, "SELECT request_json FROM project_todo_events WHERE todo_id=? ORDER BY event_id LIMIT 1", (old["todo_id"],))
                if old["project_id"] != str(project_id) or event[0] != canonical(value.model_dump(mode="json")):
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return dict(old)
            if (await one(conn, "SELECT count(*) FROM project_todos WHERE project_id=?", (str(project_id),)))[0] >= 1000 or (await one(conn, "SELECT count(*) FROM project_todos"))[0] >= 10000:
                raise Conflict("BUSY")
            identifier = str(uuid4())
            await conn.execute("INSERT INTO project_todos(todo_id,project_id,owner_id,request_id,text,status,created_at,updated_at) VALUES(?,?,?,?,?,'planned',?,?)", (identifier, str(project_id), str(principal.identity_id), str(value.request_id), value.text, timestamp(now), timestamp(now)))
            return await self._record(conn, principal, identifier, value, now)

    async def _record(self, conn, principal, identifier, value, now):
        row = dict(await one(conn, "SELECT * FROM project_todos WHERE todo_id=?", (identifier,)))
        await conn.execute("INSERT INTO project_todo_events(todo_id,request_id,request_json,snapshot_json,created_at) VALUES(?,?,?,?,?)", (identifier, str(value.request_id), canonical(value.model_dump(mode="json")), canonical(row), timestamp(now)))
        await audit(conn, str(principal.identity_id), None, "project.todo_changed", now)
        return row

    async def update_todo(self, principal, project_id, todo_id, value):
        async with self.database.transaction() as conn:
            now = utc_now()
            await assert_owner(conn, principal, now)
            await project_owned(conn, project_id, principal.identity_id)
            row = await one(conn, "SELECT * FROM project_todos WHERE todo_id=? AND project_id=? AND owner_id=?", (str(todo_id), str(project_id), str(principal.identity_id)))
            if row is None:
                raise LookupError("todo not found")
            event = await one(conn, "SELECT * FROM project_todo_events WHERE todo_id=? AND request_id=?", (str(todo_id), str(value.request_id)))
            if event:
                if event["request_json"] != canonical(value.model_dump(mode="json")):
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return dict(row)
            if row["revision"] != value.expected_revision:
                raise Conflict("IDEMPOTENCY_CONFLICT")
            if (await one(conn, "SELECT count(*) FROM project_todo_events WHERE todo_id=?", (str(todo_id),)))[0] >= 200:
                raise Conflict("BUSY")
            if value.evidence_task_id:
                task = await one(conn, "SELECT * FROM tasks WHERE task_id=? AND owner_id=? AND project_id=?", (str(value.evidence_task_id), str(principal.identity_id), str(project_id)))
                if task is None or task["status"] != "succeeded" or task["execution_outcome"] != "known" or not task["verification"] or not task["result_json"]:
                    raise AccessDenied("TASK_EVIDENCE_REQUIRED")
            await conn.execute("UPDATE project_todos SET text=?,status=?,evidence_task_id=?,revision=revision+1,updated_at=? WHERE todo_id=?", (value.text, value.status, str(value.evidence_task_id) if value.evidence_task_id else None, timestamp(now), str(todo_id)))
            return await self._record(conn, principal, str(todo_id), value, now)

    async def history(self, principal, project_id, todo_id):
        async with self.database.transaction(write=False) as conn:
            await assert_owner(conn, principal, utc_now())
            await project_owned(conn, project_id, principal.identity_id)
            if not await one(conn, "SELECT 1 FROM project_todos WHERE todo_id=? AND project_id=? AND owner_id=?", (str(todo_id), str(project_id), str(principal.identity_id))):
                raise LookupError("todo not found")
            rows = await conn.execute_fetchall("SELECT * FROM project_todo_events WHERE todo_id=? ORDER BY event_id LIMIT 200", (str(todo_id),))
            return [{"event_id": row["event_id"], "created_at": row["created_at"], "snapshot": json.loads(row["snapshot_json"])} for row in rows]
