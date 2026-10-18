"""Durable chat turns sharing the existing Core authority and global ledger."""

import asyncio
import hashlib
import json
import sqlite3
from pydantic import ValidationError
from datetime import timedelta
from uuid import UUID, uuid4

from nexus.contracts.chat import CHAT_PROPOSAL, ChatActionDecision, ChatSubmission, ConversationCreate, ConversationUpdate
from nexus.core.models.budget import budget_error
from nexus.core.models.deepseek import MODEL, ProviderFailure
from nexus.core.security.types import AccessDenied, Principal
from nexus.core.security.service import scope_digest
from nexus.core.models.chat_format import ChatTitle
from nexus.core.models.providers import ModelReply
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import REQUEST, Conflict, canonical

ACTIVE = "('queued','generating','stopping')"
TURN_SELECT = "SELECT t.rowid AS cursor,t.*,m.cost_status,m.cost_microunits FROM chat_turns t LEFT JOIN model_calls m ON m.chat_turn_id=t.turn_id"


def public_turn(row: sqlite3.Row, *, detail: bool = False) -> dict:
    value = dict(row)
    request = json.loads(value.pop("request_json"))
    context = json.loads(value.pop("context_json"))
    proposal=value.pop("proposal_json")
    value.update(include_context=request["include_context"], context_truncated=bool(value["context_truncated"]),
                 allow_tools=request.get("allow_tools",False),node_id=request.get("node_id"),proposal=json.loads(proposal) if proposal else None)
    if detail:
        value["context_messages"] = context
    return value


class ChatService:
    def __init__(self, harness, provider, owner_check):
        self.harness, self.provider, self.owner = harness, provider, owner_check
        self.database, self.config = harness.security.database, harness.link.config.models
        self.active: tuple[str, asyncio.Task[ModelReply]] | None = None
        self.failed = False

    async def conversation(self, conn, principal: Principal, conversation_id: UUID | str) -> sqlite3.Row:
        await self.owner(conn, principal)
        row = await one(conn, "SELECT rowid AS cursor,* FROM conversations WHERE conversation_id=? AND owner_id=?", (str(conversation_id), str(principal.identity_id)))
        if row is None:
            raise LookupError("conversation not found")
        return row

    async def create(self, principal: Principal, value: ConversationCreate) -> dict:
        async with self.database.transaction() as conn:
            await self.owner(conn, principal)
            old = await one(conn, "SELECT * FROM conversations WHERE owner_id=? AND request_id=?", (str(principal.identity_id), str(value.request_id)))
            if old:
                return dict(old)
            if (await one(conn, "SELECT count(*) FROM conversations"))[0] >= 100:
                raise Conflict("BUSY")
            identifier, now = str(uuid4()), utc_now()
            await conn.execute("INSERT INTO conversations(conversation_id,owner_id,request_id,title,created_at,auto_title) VALUES(?,?,?,'新会话',?,1)", (identifier, str(principal.identity_id), str(value.request_id), timestamp(now)))
            await audit(conn, str(principal.identity_id), None, "chat.created", now)
            return dict(await one(conn, "SELECT * FROM conversations WHERE conversation_id=?", (identifier,)))

    async def page(self, principal: Principal, before: int = 2**63-1, archived: bool = False) -> list[dict]:
        async with self.database.transaction(write=False) as conn:
            await self.owner(conn, principal)
            rows = await conn.execute_fetchall("SELECT rowid AS cursor,* FROM conversations WHERE owner_id=? AND rowid<? AND archived=? ORDER BY rowid DESC LIMIT 20", (str(principal.identity_id), before, int(archived)))
            return [dict(row) for row in rows]

    async def update(self, principal: Principal, conversation_id: UUID | str, value: ConversationUpdate) -> dict:
        async with self.database.transaction() as conn:
            row = await self.conversation(conn, principal, conversation_id)
            await conn.execute("UPDATE conversations SET title=?,archived=?,revision=revision+1 WHERE conversation_id=?", (
                value.title.strip() if value.title is not None else row["title"], int(value.archived) if value.archived is not None else row["archived"], str(conversation_id)))
            if value.title is not None:
                await conn.execute("UPDATE conversations SET auto_title=0 WHERE conversation_id=?",(str(conversation_id),))
            await audit(conn, str(principal.identity_id), None, "chat.updated", utc_now())
            return dict(await one(conn, "SELECT * FROM conversations WHERE conversation_id=?", (str(conversation_id),)))

    async def history(self, principal: Principal, conversation_id: UUID | str, before: int = 2**63-1) -> dict:
        async with self.database.transaction(write=False) as conn:
            conversation = await self.conversation(conn, principal, conversation_id)
            rows = await conn.execute_fetchall(TURN_SELECT + " WHERE t.conversation_id=? AND t.rowid<? ORDER BY t.rowid DESC LIMIT 20", (str(conversation_id), before))
            active = await one(conn, f"SELECT turn_id FROM chat_turns WHERE conversation_id=? AND status IN {ACTIVE}", (str(conversation_id),))
            return {"conversation": dict(conversation), "active_turn_id": active[0] if active else None, "turns": [public_turn(row) for row in reversed(rows)]}

    async def get(self, principal: Principal, turn_id: UUID | str) -> dict:
        async with self.database.transaction(write=False) as conn:
            await self.owner(conn, principal)
            row = await one(conn, TURN_SELECT + " WHERE t.turn_id=? AND t.owner_id=?", (str(turn_id), str(principal.identity_id)))
            if row is None:
                raise LookupError("chat turn not found")
            return public_turn(row, detail=True)

    async def submit(self, principal: Principal, conversation_id: UUID | str, value: ChatSubmission) -> dict:
        value = ChatSubmission.model_validate_json(value.model_dump_json())
        encoded = canonical(value.model_dump(mode="json"))
        async with self.database.transaction() as conn:
            conversation = await self.conversation(conn, principal, conversation_id)
            old = await one(conn, TURN_SELECT + " WHERE t.owner_id=? AND t.request_id=?", (str(principal.identity_id), str(value.request_id)))
            if old:
                if old["conversation_id"] != str(conversation_id) or old["request_json"] != encoded:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return public_turn(old)
            if not value.allow_model:
                raise AccessDenied("MODEL_CONSENT_REQUIRED")
            if not self.config.enabled or not self.config.chat_enabled or self.provider is None:
                raise AccessDenied("MODEL_DISABLED")
            if self.failed or self.harness.failed:
                raise AccessDenied("BUSY")
            if value.allow_tools:
                self.harness.scope(value.node_id)
                if not await one(conn,"SELECT 1 FROM identities WHERE identity_id=? AND role='node' AND revoked_at IS NULL",(str(value.node_id),)):
                    raise AccessDenied("POLICY_DENIED")
            if conversation["archived"] or conversation["revision"] != value.expected_revision:
                raise Conflict("IDEMPOTENCY_CONFLICT")
            if await one(conn, f"SELECT 1 FROM chat_turns WHERE conversation_id=? AND status IN {ACTIVE}", (str(conversation_id),)):
                raise Conflict("BUSY")
            now = utc_now()
            recent = await one(conn, "SELECT count(*) FROM chat_turns WHERE owner_id=? AND created_at>?", (str(principal.identity_id), timestamp(now-timedelta(minutes=1))))
            total = await one(conn, "SELECT count(*),coalesce(sum(length(CAST(context_json AS BLOB))+length(CAST(response_text AS BLOB))+length(CAST(request_json AS BLOB))),0) FROM chat_turns")
            count = (await one(conn, "SELECT count(*) FROM chat_turns WHERE conversation_id=?", (str(conversation_id),)))[0]
            if recent[0] >= 10 or total[0] >= 10000 or total[1] >= 268000000 or count >= 200:
                raise Conflict("BUSY")
            messages = [{"role": "user", "content": value.text}]
            selected = 0
            available = 0
            if value.include_context:
                available = (await one(conn, "SELECT count(*) FROM chat_turns WHERE conversation_id=? AND status='completed'", (str(conversation_id),)))[0]
                rows = await conn.execute_fetchall("SELECT text,response_text FROM chat_turns WHERE conversation_id=? AND status='completed' ORDER BY rowid DESC LIMIT 10", (str(conversation_id),))
                for row in rows:
                    candidate = [{"role": "user", "content": row["text"]}, {"role": "assistant", "content": row["response_text"]}, *messages]
                    if len(canonical(candidate).encode()) > 24576:
                        break
                    messages, selected = candidate, selected+1
            context = canonical(messages)
            turn_id = str(uuid4())
            await conn.execute("INSERT INTO chat_turns(turn_id,conversation_id,owner_id,request_id,request_json,text,context_json,context_digest,context_count,context_truncated,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?)", (
                turn_id, str(conversation_id), str(principal.identity_id), str(value.request_id), encoded, value.text, context,
                hashlib.sha256(context.encode()).hexdigest(), selected, int(available > selected), timestamp(now), timestamp(now+timedelta(minutes=5))))
            await conn.execute("UPDATE conversations SET revision=revision+1,title=? WHERE conversation_id=?", (" ".join(value.text.split())[:40] if count == 0 and conversation["auto_title"] else conversation["title"], str(conversation_id)))
            await audit(conn, str(principal.identity_id), None, "chat.consented", now)
            return public_turn(await one(conn, TURN_SELECT + " WHERE t.turn_id=?", (turn_id,)))

    def action_digest(self, row, proposal, policy_version):
        request=json.loads(row["request_json"])
        node_id=UUID(request["node_id"])
        return hashlib.sha256(canonical({"turn_id":row["turn_id"],"node_id":str(node_id),"proposal":proposal,
            "tool_version":1,"policy_version":policy_version,"scope_digest":scope_digest(self.harness.scope(node_id)),
            "expires_at":row["expires_at"]}).encode()).hexdigest()

    async def decide_action(self, principal: Principal, turn_id: UUID, value: ChatActionDecision):
        async with self.harness.operation:
            async with self.database.transaction() as conn:
                await self.owner(conn,principal)
                row=await one(conn,"SELECT t.*,c.archived FROM chat_turns t JOIN conversations c USING(conversation_id) WHERE turn_id=? AND t.owner_id=?",(str(turn_id),str(principal.identity_id)))
                if row is None:raise LookupError("chat turn not found")
                if not row["proposal_digest"] or row["proposal_digest"]!=value.proposal_digest:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                if row["task_id"]:
                    if value.decision!="confirm":raise Conflict("IDEMPOTENCY_CONFLICT")
                    return {"task":await self.harness.tasks.get(UUID(row["task_id"]),principal.identity_id)}
                if value.decision=="reject":
                    await conn.execute("UPDATE chat_turns SET action_status='rejected',revision=revision+1 WHERE turn_id=? AND action_status='proposed'",(str(turn_id),))
                    await audit(conn,str(principal.identity_id),None,"chat.action_rejected",utc_now())
                    return {"task":None}
                if row["action_status"]!="proposed" or row["status"]!="completed" or row["archived"] or row["expires_at"]<=timestamp(utc_now()):
                    raise AccessDenied("APPROVAL_EXPIRED")
                if self.failed or self.harness.failed or (await one(conn,"SELECT paused FROM system_state WHERE singleton=1"))[0]:
                    raise AccessDenied("BUSY")
                proposal=CHAT_PROPOSAL.validate_json(row["proposal_json"]).model_dump(mode="json")
                version=(await one(conn,"SELECT version FROM policy_state WHERE singleton=1"))[0]
                if self.action_digest(row,proposal,version)!=value.proposal_digest:
                    raise AccessDenied("POLICY_DENIED")
                node_id=UUID(json.loads(row["request_json"])["node_id"])
                request=REQUEST.validate_python({**proposal,"request_id":turn_id})
            # Task creation and association commit together; no model call here.
            task=await self.harness.submit(principal,node_id,request,chat_turn_id=turn_id)
            return {"task":task}

    async def cancel(self, principal: Principal, turn_id: UUID | str) -> dict:
        async with self.harness.operation:
            async with self.database.transaction() as conn:
                await self.owner(conn, principal)
                row = await one(conn, "SELECT * FROM chat_turns WHERE turn_id=? AND owner_id=?", (str(turn_id), str(principal.identity_id)))
                if row is None:
                    raise LookupError("turn not found")
                if row["status"] in {"queued", "generating"}:
                    await conn.execute("UPDATE chat_turns SET status=?,revision=revision+1 WHERE turn_id=?", ("stopped" if row["status"] == "queued" else "stopping", str(turn_id)))
                    await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id=?", (row["conversation_id"],))
                    await audit(conn, str(principal.identity_id), None, "chat.stop_requested", utc_now())
            if self.active and self.active[0] == str(turn_id):
                self.active[1].cancel()
        return await self.get(principal, turn_id)

    async def recover(self) -> None:
        async with self.database.transaction() as conn:
            await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id IN (SELECT conversation_id FROM chat_turns WHERE status IN ('generating','stopping'))")
            await conn.execute("UPDATE model_calls SET cost_status='unknown',outcome='unknown',finished_at=? WHERE chat_turn_id IS NOT NULL AND outcome='started'", (timestamp(utc_now()),))
            await conn.execute("UPDATE chat_turns SET status=CASE WHEN status='stopping' THEN 'stopped' ELSE 'interrupted' END,revision=revision+1,error_code='MODEL_OUTCOME_UNKNOWN' WHERE status IN ('generating','stopping')")

    async def delta(self, turn_id: str, text: str) -> None:
        async with self.database.transaction() as conn:
            cursor = await conn.execute("UPDATE chat_turns SET response_text=?,revision=revision+1 WHERE turn_id=? AND status='generating'", (text, turn_id))
            if cursor.rowcount != 1:
                raise asyncio.CancelledError()

    async def claim(self):
        if self.failed or self.harness.failed:
            return None
        async with self.database.transaction(write=False) as conn:
            if await one(conn, "SELECT 1 FROM chat_turns WHERE status='queued' LIMIT 1") is None:
                return None
        async with self.harness.operation:
            async with self.database.transaction() as conn:
                now = utc_now()
                if self.failed or self.harness.failed or self.active is not None or (await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0]:
                    return None
                if await one(conn, "SELECT 1 FROM model_calls WHERE outcome='started' LIMIT 1"):
                    return None
                row = await one(conn, "SELECT t.*,(c.auto_title=1 AND NOT EXISTS(SELECT 1 FROM chat_turns older WHERE older.conversation_id=t.conversation_id AND older.rowid<t.rowid)) AS generate_title FROM chat_turns t JOIN conversations c USING(conversation_id) WHERE t.status='queued' ORDER BY t.rowid LIMIT 1")
                if row is None:
                    return None
                owner = await one(conn, "SELECT 1 FROM identities WHERE identity_id=? AND role='owner' AND revoked_at IS NULL", (row["owner_id"],))
                error = "AUTH_REQUIRED" if not owner else "MODEL_DISABLED" if not self.config.chat_enabled or self.provider is None else await budget_error(conn, self.config, now)
                if row["expires_at"] <= timestamp(now):
                    error = "DEADLINE_EXCEEDED"
                if error:
                    await conn.execute("UPDATE chat_turns SET status=?,error_code=?,revision=revision+1 WHERE turn_id=?", ("expired" if error == "DEADLINE_EXCEEDED" else "failed", error, row["turn_id"]))
                    await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id=?", (row["conversation_id"],))
                    return None
                call_id = str(uuid4())
                await conn.execute("INSERT INTO model_calls(call_id,provider,model,cost_status,created_at,owner_id,reserved_microunits,pricing_json,outcome,chat_turn_id) VALUES(?,'deepseek',?,'reserved',?,?,?,?, 'started',?)", (
                    call_id, MODEL, timestamp(now), row["owner_id"], self.config.task_cost_microunits,
                    canonical({"input": self.config.input_price, "output": self.config.output_price, "currency": "USD", "valid_until": self.config.price_valid_until.isoformat()}), row["turn_id"]))
                await conn.execute("UPDATE chat_turns SET status='generating',revision=revision+1 WHERE turn_id=?", (row["turn_id"],))
                await audit(conn, row["owner_id"], None, "model.reserved", now)
            # HTTP starts only after the transaction committed, ordered with controls.
            accumulated, last_flush = "", 0.0

            async def receive(part: str) -> None:
                nonlocal accumulated, last_flush
                accumulated += part
                if len(accumulated.encode()) > 32768:
                    raise ProviderFailure("MODEL_RESPONSE_LIMIT")
                clock = asyncio.get_running_loop().time()
                if clock-last_flush >= .1:
                    await self.delta(row["turn_id"], accumulated)
                    last_flush = clock

            request=json.loads(row["request_json"])
            task = asyncio.create_task(self.provider.invoke(json.loads(row["context_json"]), receive,generate_title=bool(row["generate_title"]),allow_tools=request.get("allow_tools",False)))
            self.active = row["turn_id"], task
            return row, call_id, task, lambda: accumulated

    async def finish(self, row: sqlite3.Row, call_id: str, reply: ModelReply | None, partial: str, *, interrupted: bool = False) -> None:
        cost, error, status = None, "MODEL_OUTCOME_UNKNOWN", "failed"
        if reply is not None:
            cost = (reply.input_tokens*self.config.input_price + reply.output_tokens*self.config.output_price + 999999)//1000000
            if cost <= self.config.task_cost_microunits and len(reply.content.encode()) <= 32768:
                if reply.complete and (reply.content.strip() or reply.proposal is not None):
                    status, error = "completed", None
                elif reply.finish_reason == "length":
                    status, error = "incomplete", "MODEL_OUTPUT_LIMIT"
                else:
                    error = "MODEL_RESPONSE_INVALID"
            else:
                cost, reply, error = None, None, "MODEL_USAGE_INVALID"
        async with self.database.transaction() as conn:
            now = utc_now()
            current = await one(conn, "SELECT status FROM chat_turns WHERE turn_id=?", (row["turn_id"],))
            if current[0] == "stopping":
                status, error = "stopped", None
            elif interrupted:
                status, error = "interrupted", "MODEL_OUTCOME_UNKNOWN"
            proposal=None
            if status=="completed" and reply is not None and reply.proposal is not None:
                try:
                    proposal=CHAT_PROPOSAL.validate_python(reply.proposal).model_dump(mode="json")
                    if not json.loads(row["request_json"]).get("allow_tools"):
                        raise AccessDenied("POLICY_DENIED")
                    version=(await one(conn,"SELECT version FROM policy_state WHERE singleton=1"))[0]
                    digest=self.action_digest(row,proposal,version)
                except (ValidationError,AccessDenied):
                    proposal=None
                    status,error="failed","POLICY_DENIED"
            await conn.execute("UPDATE model_calls SET cost_microunits=?,cost_status=?,input_tokens=?,output_tokens=?,finished_at=?,outcome=?,model=? WHERE call_id=? AND outcome='started'", (
                cost, "calculated" if cost is not None else "unknown", reply.input_tokens if reply else None, reply.output_tokens if reply else None,
                timestamp(now), "completed" if reply else "unknown", reply.model[:200] if reply else MODEL, call_id))
            if current[0] in {"generating", "stopping"}:
                await conn.execute("UPDATE chat_turns SET response_text=?,status=?,error_code=?,revision=revision+1 WHERE turn_id=?", (reply.content if reply else partial, status, error, row["turn_id"]))
                await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id=?", (row["conversation_id"],))
                if status=="completed" and reply is not None:
                    if row["generate_title"] and reply.title:
                        try:
                            title=ChatTitle(title=reply.title).title.strip()
                        except ValidationError:
                            title=None
                        if title:
                            await conn.execute("UPDATE conversations SET title=?,auto_title=0 WHERE conversation_id=? AND auto_title=1",(title,row["conversation_id"]))
                    if proposal is not None:
                        await conn.execute("UPDATE chat_turns SET proposal_json=?,proposal_digest=?,proposal_policy_version=?,action_status='proposed' WHERE turn_id=?",(canonical(proposal),digest,version,row["turn_id"]))
            await audit(conn, row["owner_id"], None, "model.settled" if reply else "model.unknown", now)

    async def process_one(self) -> bool:
        item = await self.claim()
        if item is None:
            return False
        row, call_id, task, partial = item
        reply = None
        try:
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=.2)
                    if not task.done():
                        async with self.database.transaction(write=False) as conn:
                            current = await one(conn, "SELECT status FROM chat_turns WHERE turn_id=?", (row["turn_id"],))
                        if current[0] != "generating" or self.harness.failed:
                            task.cancel()
                reply = await task
            except ProviderFailure:
                pass
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if asyncio.current_task().cancelling():
                    await self.finish(row, call_id, None, partial(), interrupted=True)
                    raise
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            await self.finish(row, call_id, reply, partial())
        finally:
            self.active = None
        return True

    async def run(self) -> None:
        while True:
            try:
                await self.process_one()
            except (sqlite3.Error, OSError):
                self.failed = self.harness.failed = True
                self.harness.wake.set()
                return
            await asyncio.sleep(.25)
