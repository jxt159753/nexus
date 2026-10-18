"""One durable cursor for task and control invalidations, replayed in pages."""

from uuid import UUID

from nexus.contracts.messages import ApprovalRequired, ClientSynced, ControlUpdated, TaskUpdated
from nexus.core.storage.database import Database, one, timestamp, utc_now
from nexus.core.storage.tasks import load_task, task_record


class ClientEvents:
    def __init__(self, database: Database):
        self.database = database

    async def sync(self, owner_id: UUID, cursor: str | None) -> tuple[int, ClientSynced]:
        async with self.database.transaction(write=False) as conn:
            high = (await one(conn, "SELECT coalesce(max(event_id),0) FROM client_events"))[0]
            valid = cursor is not None and int(cursor) <= high
            if valid and int(cursor) != 0:
                valid = await one(conn, "SELECT 1 FROM client_events WHERE event_id=?", (int(cursor),)) is not None
            start = int(cursor) if valid else high
            return start, ClientSynced(event_id=str(start), full_sync_required=not valid)

    async def page(self, owner_id: UUID, cursor: int, limit: int = 16) -> tuple[list[tuple[str, object]], int]:
        if not 1 <= limit <= 32 or not 0 <= cursor <= 2**63-1:
            raise ValueError("invalid event page")
        messages = []
        async with self.database.transaction(write=False) as conn:
            rows = await conn.execute_fetchall("SELECT c.event_id,e.task_id,e.kind,k.paused,k.revision FROM client_events c LEFT JOIN task_events e ON e.event_id=c.task_event_id LEFT JOIN tasks t ON t.task_id=e.task_id LEFT JOIN control_events k ON k.event_id=c.control_event_id WHERE c.event_id>? AND (t.owner_id=? OR k.owner_id=?) ORDER BY c.event_id LIMIT ?", (cursor, str(owner_id), str(owner_id), limit))
            for row in rows:
                event_id = str(row["event_id"])
                if row["task_id"] is None:
                    messages.append(("control.updated", ControlUpdated(event_id=event_id, paused=bool(row["paused"]), revision=row["revision"])))
                else:
                    task = await load_task(conn, UUID(row["task_id"]))
                    approval = None
                    if task.status == "awaiting_approval" and row["kind"] in {"awaiting_approval", "approval.required"}:
                        approval = await one(conn, "SELECT * FROM approvals WHERE task_id=? AND decision IS NULL AND expires_at>? ORDER BY rowid DESC LIMIT 1", (row["task_id"], timestamp(utc_now())))
                    if approval:
                        payload = ApprovalRequired(event_id=event_id, approval_id=UUID(approval["approval_id"]), task_id=task.task_id, node_id=task.node_id, action=task.action, expires_at=approval["expires_at"])
                        messages.append(("approval.required", payload))
                    else:
                        # Events invalidate a task. Return its latest committed snapshot,
                        # never attach today's result to an old revision/status.
                        messages.append(("task.updated", TaskUpdated(event_id=event_id, task_id=task.task_id, revision=task.revision, status=task.status, execution_outcome=task.execution_outcome, result=task.result, error=task.error, verification=task.verification)))
            return messages, rows[-1]["event_id"] if rows else cursor

    async def snapshot(self, owner_id: UUID, before: int = 2**63-1) -> dict:
        async with self.database.transaction(write=False) as conn:
            rows = await conn.execute_fetchall("SELECT rowid AS cursor,* FROM tasks WHERE owner_id=? AND rowid<? ORDER BY rowid DESC LIMIT 21", (str(owner_id), before))
            tasks = [task_record(row) for row in rows[:20]]
            state = await one(conn, "SELECT paused,revision FROM system_state WHERE singleton=1")
            return {"tasks": tasks, "next_before": str(rows[min(len(rows),20)-1]["cursor"]) if rows else str(before), "has_more": len(rows) > 20, "control": {"paused": bool(state["paused"]), "revision": state["revision"]}}
