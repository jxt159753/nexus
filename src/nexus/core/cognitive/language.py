"""Durable interpretation, atomic cost reservations and explicit action confirmation."""

import asyncio
import json
import sqlite3
from datetime import timedelta
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, ValidationError

from nexus.contracts.language import DirectoryProposal, LanguageSubmission, SystemProposal, UnsupportedProposal
from nexus.core.models.deepseek import MODEL, ProviderFailure
from nexus.core.models.providers import ModelReply
from nexus.core.models.budget import budget_error
from nexus.core.security.service import assert_owner
from nexus.core.security.types import AccessDenied, Principal
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import REQUEST, Conflict, canonical

PROPOSAL = TypeAdapter(Annotated[SystemProposal | DirectoryProposal | UnsupportedProposal, Field(discriminator="intent")])
ACTIVE = "('queued','interpreting','proposed')"


def rule(text: str) -> dict | None:
    value = text.strip().casefold()
    if value in {"读取系统信息", "获取系统信息", "查看系统信息", "get system info"}:
        return {"intent": "get_system_info", "arguments": {}}
    if value in {"列出 workspace 目录", "查看 workspace 目录", "list workspace"}:
        return {"intent": "list_directory", "arguments": {"root_id": "workspace", "limit": 100}}
    return None


def public(row) -> dict:
    value = dict(row)
    request = json.loads(value.pop("request_json"))
    value["text"] = request["text"]
    value["allow_model"] = request["allow_model"]
    proposal = value.pop("proposal_json")
    value["proposal"] = json.loads(proposal) if proposal else None
    return value


class LanguageService:
    def __init__(self, harness, provider):
        self.harness, self.provider = harness, provider
        self.database = harness.security.database
        self.config = harness.link.config.models
        self.wake = asyncio.Event()
        self.active: tuple[str, asyncio.Task] | None = None
        self.failed = False

    async def recover(self) -> None:
        # The remote service may already have billed a request when Core died.
        async with self.database.transaction() as conn:
            await conn.execute("UPDATE model_calls SET cost_status='unknown',outcome='unknown',finished_at=? WHERE outcome='started'", (timestamp(utc_now()),))
            await conn.execute("UPDATE language_requests SET status='failed',error_code='MODEL_OUTCOME_UNKNOWN' WHERE status='interpreting'")

    async def owner(self, conn, principal: Principal) -> None:
        await assert_owner(conn, principal, utc_now())
        if principal.identity_id != self.harness.link.config.owner_id:
            raise AccessDenied()

    async def submit(self, principal: Principal, value: LanguageSubmission) -> dict:
        value = LanguageSubmission.model_validate_json(value.model_dump_json())
        self.harness.scope(value.node_id)
        encoded = canonical(value.model_dump(mode="json"))
        async with self.database.transaction() as conn:
            now = utc_now()
            await self.owner(conn, principal)
            old = await one(conn, "SELECT * FROM language_requests WHERE owner_id=? AND request_id=?", (str(principal.identity_id), str(value.request_id)))
            if old:
                if old["request_json"] != encoded:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return public(old)
            if self.failed or self.harness.failed:
                raise AccessDenied("BUSY")
            node = await one(conn, "SELECT 1 FROM identities WHERE identity_id=? AND role='node' AND revoked_at IS NULL", (str(value.node_id),))
            if not node:
                raise AccessDenied()
            if value.project_id and not await one(conn, "SELECT 1 FROM projects WHERE project_id=? AND owner_id=?", (str(value.project_id), str(principal.identity_id))):
                raise LookupError("project not found")
            recent = await one(conn, "SELECT count(*) FROM language_requests WHERE owner_id=? AND created_at>?", (str(principal.identity_id), timestamp(now-timedelta(minutes=1))))
            size = await one(conn, f"SELECT count(*),coalesce(sum(status IN {ACTIVE}),0) FROM language_requests")
            if recent[0] >= 10 or size[0] >= 10000 or size[1] >= 100:
                raise Conflict("BUSY")
            language_id = str(uuid4())
            await conn.execute("INSERT INTO language_requests(language_id,owner_id,request_id,node_id,project_id,request_json,status,created_at,expires_at) VALUES(?,?,?,?,?,?,'queued',?,?)", (
                language_id, str(principal.identity_id), str(value.request_id), str(value.node_id), str(value.project_id) if value.project_id else None,
                encoded, timestamp(now), timestamp(now+timedelta(minutes=5))))
            await audit(conn, str(principal.identity_id), None, "language.created", now)
            row = await one(conn, "SELECT * FROM language_requests WHERE language_id=?", (language_id,))
        self.wake.set()
        return public(row)

    async def get(self, principal: Principal, language_id: UUID) -> dict:
        async with self.database.transaction(write=False) as conn:
            await self.owner(conn, principal)
            row = await one(conn, "SELECT * FROM language_requests WHERE language_id=? AND owner_id=?", (str(language_id), str(principal.identity_id)))
            if not row:
                raise LookupError("language request not found")
            return public(row)

    async def list(self, principal: Principal, before: int = 2**63-1) -> list[dict]:
        async with self.database.transaction(write=False) as conn:
            await self.owner(conn, principal)
            rows = await conn.execute_fetchall("SELECT rowid AS cursor,* FROM language_requests WHERE owner_id=? AND rowid<? ORDER BY rowid DESC LIMIT 20", (str(principal.identity_id), before))
            return [public(row) for row in rows]

    async def cancel(self, principal: Principal, language_id: UUID) -> dict:
        async with self.harness.operation:
            async with self.database.transaction() as conn:
                await self.owner(conn, principal)
                row = await one(conn, "SELECT * FROM language_requests WHERE language_id=? AND owner_id=?", (str(language_id), str(principal.identity_id)))
                if not row:
                    raise LookupError("language request not found")
                if row["status"] in {"queued", "interpreting", "proposed"}:
                    await conn.execute("UPDATE language_requests SET status='cancelled' WHERE language_id=?", (str(language_id),))
                    await audit(conn, str(principal.identity_id), None, "language.cancelled", utc_now())
        if self.active and self.active[0] == str(language_id):
            self.active[1].cancel()
        return await self.get(principal, language_id)

    async def confirm(self, principal: Principal, language_id: UUID) -> dict:
        async with self.harness.operation:
            value = await self.get(principal, language_id)
            if value["task_id"]:
                return {"task": await self.harness.tasks.get(UUID(value["task_id"]), principal.identity_id)}
            if value["status"] != "proposed" or value["expires_at"] <= timestamp(utc_now()):
                raise AccessDenied("APPROVAL_EXPIRED")
            if self.failed or self.harness.failed or (await self.harness.controls.state())["paused"]:
                raise AccessDenied("BUSY")
            # Stable Core-generated identity makes confirmation crash/retry safe.
            request = REQUEST.validate_json(canonical({**value["proposal"], "request_id": str(language_id), "project_id": value["project_id"]}))
            task = await self.harness.submit(principal, UUID(value["node_id"]), request, language_id=language_id)
            return {"task": task}

    async def claim(self):
        if self.failed or self.harness.failed:
            return None
        # An idle interpreter must not continually acquire SQLite's writer lock.
        async with self.database.transaction(write=False) as conn:
            work = await one(conn, f"SELECT 1 FROM language_requests WHERE status='queued' OR (status IN {ACTIVE} AND expires_at<=?) LIMIT 1", (timestamp(utc_now()),))
            if work is None or (await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0]:
                return None
        async with self.harness.operation:
            async with self.database.transaction() as conn:
                now = utc_now()
                await conn.execute(f"UPDATE language_requests SET status='expired',error_code='DEADLINE_EXCEEDED' WHERE status IN {ACTIVE} AND expires_at<=?", (timestamp(now),))
                if self.failed or self.harness.failed or (await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0]:
                    return None
                if self.active is not None or (await one(conn, "SELECT count(*) FROM model_calls WHERE outcome='started'"))[0]:
                    return None
                row = await one(conn, "SELECT * FROM language_requests WHERE status='queued' ORDER BY created_at LIMIT 1")
                if not row:
                    return None
                owner = await one(conn, "SELECT 1 FROM identities WHERE identity_id=? AND revoked_at IS NULL AND role='owner'", (row["owner_id"],))
                request = json.loads(row["request_json"])
                proposal = rule(request["text"])
                error = None
                if not owner:
                    error = "AUTH_REQUIRED"
                elif proposal:
                    await conn.execute("UPDATE language_requests SET status='proposed',mode='rule',proposal_json=? WHERE language_id=?", (canonical(proposal), row["language_id"]))
                    return None
                elif not request["allow_model"]:
                    error = "MODEL_CONSENT_REQUIRED"
                elif not self.config.enabled or self.provider is None:
                    error = "MODEL_DISABLED"
                else:
                    error = await budget_error(conn, self.config, now, reservation=self.config.model_reservation(input_tokens=16384, output_tokens=self.config.max_output_tokens))
                if error:
                    await conn.execute("UPDATE language_requests SET status='failed',error_code=? WHERE language_id=?", (error, row["language_id"]))
                    return None
                call_id = str(uuid4())
                await conn.execute("INSERT INTO model_calls(call_id,provider,model,cost_status,created_at,language_id,owner_id,reserved_microunits,pricing_json,outcome) VALUES(?,'deepseek',?,'reserved',?,?,?,?,?,'started')", (
                    call_id, MODEL, timestamp(now), row["language_id"], row["owner_id"], self.config.model_reservation(input_tokens=16384, output_tokens=self.config.max_output_tokens),
                    canonical({"input": self.config.input_price, "output": self.config.output_price, "currency": "USD", "valid_until": self.config.price_valid_until.isoformat()})))
                await conn.execute("UPDATE language_requests SET status='interpreting',mode='llm' WHERE language_id=?", (row["language_id"],))
                await audit(conn, row["owner_id"], None, "model.reserved", now)
            # Commit the reservation before the HTTP coroutine can run. Keep the
            # operation lock until its start is ordered with pause/cancel_all.
            task = asyncio.create_task(self.provider.invoke(request["text"]))
            self.active = row["language_id"], task
            return row, call_id, task

    async def finish(self, row, call_id: str, reply: ModelReply | None) -> None:
        proposal, error, cost = None, "MODEL_OUTCOME_UNKNOWN", None
        if reply is not None:
            cost = (reply.input_tokens*self.config.input_price + reply.output_tokens*self.config.output_price + 999999)//1000000
            error = "MODEL_RESPONSE_INVALID"
            try:
                parsed = PROPOSAL.validate_json(reply.content)
                if reply.complete and (not self.config.cost_limits_enabled or cost <= self.config.task_cost_microunits):
                    if isinstance(parsed, UnsupportedProposal):
                        error = "UNSUPPORTED_TASK"
                    else:
                        proposal, error = parsed.model_dump(), None
            except ValidationError:
                pass
        now = utc_now()
        async with self.database.transaction() as conn:
            await conn.execute("UPDATE model_calls SET cost_microunits=?,cost_status=?,input_tokens=?,output_tokens=?,finished_at=?,outcome=? WHERE call_id=? AND outcome='started'", (
                cost, "calculated" if cost is not None else "unknown", reply.input_tokens if reply else None,
                reply.output_tokens if reply else None, timestamp(now), "completed" if reply else "unknown", call_id))
            if reply is not None:
                await conn.execute("UPDATE model_calls SET model=? WHERE call_id=?", (reply.model[:200], call_id))
            current = await one(conn, "SELECT status,expires_at FROM language_requests WHERE language_id=?", (row["language_id"],))
            if current["status"] == "interpreting":
                status = "proposed" if proposal else "rejected" if error == "UNSUPPORTED_TASK" else "failed"
                if current["expires_at"] <= timestamp(now):
                    status, proposal, error = "expired", None, "DEADLINE_EXCEEDED"
                await conn.execute("UPDATE language_requests SET status=?,proposal_json=?,error_code=? WHERE language_id=?", (status, canonical(proposal) if proposal else None, error, row["language_id"]))
            await audit(conn, row["owner_id"], None, "model.settled" if reply else "model.unknown", now)

    async def process_one(self) -> bool:
        item = await self.claim()
        if item is None:
            return False
        row, call_id, task = item
        reply = None
        try:
            # Global cancellation is durable; close the actual HTTP exchange too.
            while not task.done():
                await asyncio.wait({task}, timeout=.25)
                if not task.done():
                    async with self.database.transaction(write=False) as conn:
                        current = await one(conn, "SELECT status FROM language_requests WHERE language_id=?", (row["language_id"],))
                    if current[0] != "interpreting":
                        task.cancel()
            reply = await task
        except ProviderFailure:
            pass
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await self.finish(row, call_id, None)
                raise
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            self.active = None
        await self.finish(row, call_id, reply)
        return True

    async def run(self) -> None:
        while True:
            try:
                await self.process_one()
            except (sqlite3.Error, OSError):
                self.failed = self.harness.failed = True
                self.harness.wake.set()
                return
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), .25)
            except TimeoutError:
                pass
