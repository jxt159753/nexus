"""Atomic consent, bounded queries and soft retraction; no model access."""

from uuid import UUID, uuid4

from nexus.contracts.records import MemoryCreate, ProjectCreate
from nexus.core.security.service import assert_owner
from nexus.core.security.types import Principal
from nexus.core.storage.database import Database, audit, one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict, canonical


async def project_owned(conn, project_id: UUID | None, owner_id: UUID) -> None:
    if project_id is not None and not await one(conn, "SELECT 1 FROM projects WHERE project_id=? AND owner_id=?", (str(project_id), str(owner_id))):
        raise LookupError("project not found")


class Records:
    def __init__(self, database: Database):
        self.database = database

    async def create_project(self, principal: Principal, value: ProjectCreate) -> dict:
        value = ProjectCreate.model_validate_json(value.model_dump_json())
        async with self.database.transaction() as conn:
            now = utc_now()
            await assert_owner(conn, principal, now)
            if (await one(conn, "SELECT count(*) FROM projects"))[0] >= 1000:
                raise Conflict("BUSY")
            project_id = uuid4()
            await conn.execute("INSERT INTO projects VALUES(?,?,?,?,?)", (str(project_id), str(principal.identity_id), value.name, value.description, timestamp(now)))
            await audit(conn, str(principal.identity_id), None, "project.created", now)
            return dict(await one(conn, "SELECT * FROM projects WHERE project_id=?", (str(project_id),)))

    async def save(self, principal: Principal, value: MemoryCreate) -> dict:
        value = MemoryCreate.model_validate_json(value.model_dump_json())
        encoded = canonical(value.model_dump(mode="json"))
        async with self.database.transaction() as conn:
            now = utc_now()
            await assert_owner(conn, principal, now)
            existing = await one(conn, "SELECT * FROM memory_consents WHERE owner_id=? AND request_id=?", (str(principal.identity_id), str(value.request_id)))
            if existing:
                if existing["request_json"] != encoded:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return dict(await one(conn, "SELECT * FROM memory_entries WHERE consent_event_id=?", (existing["consent_event_id"],)))
            await project_owned(conn, value.project_id, principal.identity_id)
            if value.source.startswith("chat:"):
                origin = await one(conn, "SELECT c.project_id FROM chat_turns t JOIN conversations c USING(conversation_id) WHERE t.turn_id=? AND t.owner_id=?", (value.source[5:], str(principal.identity_id)))
                if origin is None or origin["project_id"] != (str(value.project_id) if value.project_id else None):
                    raise ValueError("memory source must belong to this project")
            if value.verified_at is not None and value.verified_at > now:
                raise ValueError("verification time cannot be in the future")
            if (await one(conn, "SELECT count(*) FROM memory_entries"))[0] >= 10000:
                raise Conflict("BUSY")
            if value.supersedes_id:
                old = await one(conn, "SELECT * FROM memory_entries WHERE memory_id=? AND owner_id=?", (str(value.supersedes_id), str(principal.identity_id)))
                if old is None:
                    raise LookupError("memory not found")
                if old["project_id"] != (str(value.project_id) if value.project_id else None):
                    raise ValueError("replacement must stay in the same project")
                if old["superseded_at"] or old["retracted_at"]:
                    raise Conflict("memory is inactive")
                await conn.execute("UPDATE memory_entries SET superseded_at=? WHERE memory_id=?", (timestamp(now), str(value.supersedes_id)))
            consent_id, memory_id = uuid4(), uuid4()
            await conn.execute("INSERT INTO memory_consents VALUES(?,?,?,?,?)", (str(consent_id), str(principal.identity_id), str(value.request_id), encoded, timestamp(now)))
            await conn.execute("INSERT INTO memory_entries(memory_id,owner_id,project_id,consent_event_id,content,kind,verification,source,evidence_ref,verified_at,valid_until,supersedes_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                str(memory_id), str(principal.identity_id), str(value.project_id) if value.project_id else None,
                str(consent_id), value.content, value.kind, value.verification, value.source, value.evidence_ref,
                timestamp(value.verified_at) if value.verified_at else None,
                timestamp(value.valid_until) if value.valid_until else None,
                str(value.supersedes_id) if value.supersedes_id else None,
                # created_at is the final column; superseded/retracted remain NULL.
                timestamp(now),
            ))
            await audit(conn, str(principal.identity_id), None, "memory.consented", now)
            return dict(await one(conn, "SELECT * FROM memory_entries WHERE memory_id=?", (str(memory_id),)))

    async def retract(self, principal: Principal, memory_id: UUID) -> dict:
        async with self.database.transaction() as conn:
            now = utc_now()
            await assert_owner(conn, principal, now)
            row = await one(conn, "SELECT * FROM memory_entries WHERE memory_id=? AND owner_id=?", (str(memory_id), str(principal.identity_id)))
            if row is None:
                raise LookupError("memory not found")
            if row["retracted_at"] is None:
                await conn.execute("UPDATE memory_entries SET retracted_at=? WHERE memory_id=?", (timestamp(now), str(memory_id)))
                await audit(conn, str(principal.identity_id), None, "memory.retracted", now)
            return dict(await one(conn, "SELECT * FROM memory_entries WHERE memory_id=?", (str(memory_id),)))

    async def page(self, principal: Principal, kind: str, *, after: int, limit: int, history: bool = False, project_id: UUID | None = None) -> dict:
        if kind not in {"projects", "memories", "routes"} or not 0 <= after <= 2**63-1 or not 1 <= limit <= 100:
            raise ValueError("invalid page")
        async with self.database.transaction(write=False) as conn:
            await assert_owner(conn, principal, utc_now())
            await project_owned(conn, project_id, principal.identity_id)
            args = [str(principal.identity_id), after]
            if kind == "routes":
                sql = "SELECT t.rowid AS cursor,r.*,t.status,t.verification,t.project_id FROM routing_decisions r JOIN tasks t USING(task_id) WHERE t.owner_id=? AND t.rowid>?"
                if project_id:
                    sql += " AND t.project_id=?"
                    args.append(str(project_id))
                sql += " ORDER BY t.rowid LIMIT ?"
            else:
                table = "memory_entries" if kind == "memories" else "projects"
                sql = f"SELECT rowid AS cursor,* FROM {table} WHERE owner_id=? AND rowid>?"
                if kind == "memories":
                    if not history:
                        sql += " AND retracted_at IS NULL AND superseded_at IS NULL AND (valid_until IS NULL OR valid_until>?)"
                        args.append(timestamp(utc_now()))
                    if project_id:
                        sql += " AND project_id=?"
                        args.append(str(project_id))
                sql += " ORDER BY rowid LIMIT ?"
            rows = await conn.execute_fetchall(sql, (*args, limit))
            items = [dict(row) for row in rows]
            cursor = str(items[-1]["cursor"] if items else after)
            for item in items:
                item.pop("cursor")
            return {kind: items, "next_after": cursor}

    async def model_usage(self, principal: Principal) -> dict:
        async with self.database.transaction(write=False) as conn:
            await assert_owner(conn, principal, utc_now())
            row = await one(conn, "SELECT count(*),coalesce(sum(cost_microunits),0),coalesce(sum(cost_status IN ('reserved','unknown') OR cost_microunits IS NULL),0) FROM model_calls")
            return {"enabled": False, "calls": row[0], "cost_microunits": row[1] if not row[2] else None, "unknown_cost_calls": row[2], "model_success_rate": None}
