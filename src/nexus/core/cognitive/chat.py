"""Durable chat turns sharing the existing Core authority and global ledger."""

import asyncio
import hashlib
import json
import sqlite3
from pydantic import ValidationError
from datetime import timedelta
from uuid import UUID, uuid4

from nexus.contracts.chat import CHAT_PROPOSAL, ChatActionDecision, ChatPlanPreview, ChatResultConsent, ChatResultPreview, ChatSubmission, ConversationCreate, ConversationUpdate
from nexus.contracts.extensions import digest_value
from nexus.core.cognitive import chat_skills
from nexus.core.cognitive.chat_work import WorkService
from nexus.core.memory import context as memory_context
from nexus.core.memory.service import project_owned
from nexus.core.cognitive.chat_results import RESULT_FIELDS, checked_native, preview, public_preview, queue_summary, validate_preview
from nexus.core.models.budget import budget_error
from nexus.core.models.deepseek import MODEL, ProviderFailure
from nexus.core.security.types import AccessDenied, Principal
from nexus.core.security.service import scope_digest
from nexus.core.models.chat_format import ChatTitle
from nexus.core.models.providers import ModelReply
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import REQUEST, Conflict, canonical

ACTIVE = "(status IN ('queued','generating','stopping') OR work_status!='' OR auto_result_status='waiting' OR skill_run_id IN (SELECT run_id FROM skill_runs WHERE status IN ('running','cancelling')))"
TURN_SELECT = """SELECT t.rowid AS cursor,t.*,m.cost_status,m.cost_microunits,coalesce(m.model_call_count,0) AS model_call_count
FROM chat_turns t LEFT JOIN (
 SELECT coalesce(chat_turn_id,work_root_turn_id) AS chat_turn_id,CASE WHEN sum(cost_status='unknown')>0 THEN 'unknown'
 WHEN sum(cost_status='reserved')>0 THEN 'reserved' ELSE 'calculated' END AS cost_status,
 sum(cost_microunits) AS cost_microunits,count(*) AS model_call_count
 FROM model_calls WHERE coalesce(chat_turn_id,work_root_turn_id) IS NOT NULL GROUP BY coalesce(chat_turn_id,work_root_turn_id)
) m ON m.chat_turn_id=t.turn_id"""


def public_turn(row: sqlite3.Row, *, detail: bool = False) -> dict:
    value = dict(row)
    value["memory_references"] = json.loads(value.pop("memory_refs_json"))
    request = json.loads(value.pop("request_json"))
    context = json.loads(value.pop("context_json"))
    proposal=value.pop("proposal_json")
    value["result_supported"] = bool(value.pop("native_tool_call_json"))
    result_preview = value.pop("result_preview_json")
    value["auto_result_allowed"] = bool(value.pop("auto_result_json"))
    catalog = json.loads(value.pop("skill_catalog_json"))
    work_catalog = json.loads(value.pop("work_catalog_json"))
    work_messages = value.pop("work_messages_json")
    plan = value.pop("skill_plan_json")
    value["skill_plan"] = json.loads(plan) if plan else None
    value.update(include_context=request["include_context"], context_truncated=bool(value["context_truncated"]),
                 allow_tools=request.get("allow_tools",False),allow_skills=request.get("allow_skills",False),node_id=request.get("node_id"),proposal=json.loads(proposal) if proposal else None)
    value["result_fields"] = ([step["id"] for step in value["skill_plan"]["steps"]] if value["skill_plan"]
                              else RESULT_FIELDS[value["proposal"]["intent"]] if value["proposal"] else ())
    if detail:
        value["work_catalog"] = work_catalog
        value["work_messages"] = json.loads(work_messages) if work_messages else None
        value["context_messages"] = context
        value["skill_catalog"] = catalog
        if result_preview and value["summary_consented_at"]:
            value["result_context_messages"] = json.loads(result_preview)["messages"]
    return value


class ChatService:
    def __init__(self, harness, provider, owner_check):
        self.harness, self.provider, self.owner = harness, provider, owner_check
        self.database, self.config = harness.security.database, harness.link.config.models
        self.active: tuple[str, asyncio.Task[ModelReply]] | None = None
        self.failed = False
        self.skills = None
        self.work = WorkService(self)

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
                if old["project_id"] != (str(value.project_id) if value.project_id else None):
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return dict(old)
            await project_owned(conn, value.project_id, principal.identity_id)
            if (await one(conn, "SELECT count(*) FROM conversations"))[0] >= 100:
                raise Conflict("BUSY")
            identifier, now = str(uuid4()), utc_now()
            await conn.execute("INSERT INTO conversations(conversation_id,owner_id,request_id,title,created_at,auto_title) VALUES(?,?,?,'新会话',?,1)", (identifier, str(principal.identity_id), str(value.request_id), timestamp(now)))
            await conn.execute("UPDATE conversations SET project_id=? WHERE conversation_id=?", (str(value.project_id) if value.project_id else None, identifier))
            await audit(conn, str(principal.identity_id), None, "chat.created", now)
            return dict(await one(conn, "SELECT * FROM conversations WHERE conversation_id=?", (identifier,)))

    async def page(self, principal: Principal, before: int = 2**63-1, archived: bool = False, project_id: UUID | None = None, project_only: bool = False) -> list[dict]:
        async with self.database.transaction(write=False) as conn:
            await self.owner(conn, principal)
            await project_owned(conn, project_id, principal.identity_id)
            sql = "SELECT rowid AS cursor,* FROM conversations WHERE owner_id=? AND rowid<? AND archived=?"
            args = [str(principal.identity_id), before, int(archived)]
            if project_only or project_id:
                sql += " AND project_id IS ?"
                args.append(str(project_id) if project_id else None)
            rows = await conn.execute_fetchall(sql + " ORDER BY rowid DESC LIMIT 20", args)
            return [dict(row) for row in rows]

    async def memory_preview(self, principal, conversation_id, value):
        async with self.database.transaction(write=False) as conn:
            conversation = await self.conversation(conn, principal, conversation_id)
            return await memory_context.selection(conn, principal.identity_id, conversation["project_id"], value.memory_ids)

    async def update(self, principal: Principal, conversation_id: UUID | str, value: ConversationUpdate) -> dict:
        async with self.database.transaction() as conn:
            row = await self.conversation(conn, principal, conversation_id)
            await conn.execute("UPDATE conversations SET title=?,archived=?,revision=revision+1 WHERE conversation_id=?", (
                value.title.strip() if value.title is not None else row["title"], int(value.archived) if value.archived is not None else row["archived"], str(conversation_id)))
            if value.title is not None:
                await conn.execute("UPDATE conversations SET auto_title=0 WHERE conversation_id=?",(str(conversation_id),))
            if value.archived:
                await conn.execute("UPDATE chat_work_grants SET revoked_at=? WHERE conversation_id=? AND revoked_at IS NULL",(timestamp(utc_now()),str(conversation_id)))
                turns=await conn.execute_fetchall("SELECT turn_id FROM chat_turns WHERE conversation_id=? AND (work_status!='' OR work_grant_id IS NOT NULL AND status IN ('queued','generating'))",(str(conversation_id),))
                for turn in turns:
                    await self.work.abort(conn,turn[0],"WORK_ARCHIVED",utc_now())
            await audit(conn, str(principal.identity_id), None, "chat.updated", utc_now())
            return dict(await one(conn, "SELECT * FROM conversations WHERE conversation_id=?", (str(conversation_id),)))

    async def history(self, principal: Principal, conversation_id: UUID | str, before: int = 2**63-1) -> dict:
        async with self.database.transaction(write=False) as conn:
            conversation = await self.conversation(conn, principal, conversation_id)
            rows = await conn.execute_fetchall(TURN_SELECT + " WHERE t.conversation_id=? AND t.rowid<? ORDER BY t.rowid DESC LIMIT 20", (str(conversation_id), before))
            active = await one(conn, f"SELECT turn_id FROM chat_turns WHERE conversation_id=? AND {ACTIVE}", (str(conversation_id),))
            value = {"conversation": dict(conversation), "active_turn_id": active[0] if active else None, "turns": [public_turn(row) for row in reversed(rows)]}
            value["work_grant"] = await self.work.describe(conn,conversation_id)
            for turn in value["turns"]:
                turn["work_steps"] = await self.work.steps(conn,turn["turn_id"])
        for turn in value["turns"]:
            if turn["skill_run_id"]:
                turn["skill_run"] = await self.skills.get(principal, UUID(turn["skill_run_id"]))
        return value

    async def get(self, principal: Principal, turn_id: UUID | str) -> dict:
        async with self.database.transaction(write=False) as conn:
            await self.owner(conn, principal)
            row = await one(conn, TURN_SELECT + " WHERE t.turn_id=? AND t.owner_id=?", (str(turn_id), str(principal.identity_id)))
            if row is None:
                raise LookupError("chat turn not found")
            value = public_turn(row, detail=True)
            value["work_steps"] = await self.work.steps(conn,turn_id)
        if value["skill_run_id"]:
            value["skill_run"] = await self.skills.get(principal, UUID(value["skill_run_id"]))
        return value

    async def skill_catalog(self, principal: Principal, node_id: UUID) -> dict:
        async with self.database.transaction(write=False) as conn:
            return await chat_skills.catalog(self, conn, principal, node_id)

    async def preview_plan(self, principal: Principal, turn_id: UUID, value: ChatPlanPreview) -> dict:
        async with self.harness.operation, self.database.transaction() as conn:
            await self.owner(conn, principal)
            row = await one(conn, "SELECT t.*,c.archived FROM chat_turns t JOIN conversations c USING(conversation_id) WHERE turn_id=? AND t.owner_id=?", (str(turn_id), str(principal.identity_id)))
            if row is None:
                raise LookupError("chat turn not found")
            if row["action_status"] != "proposed" or row["status"] != "completed" or row["archived"] or row["expires_at"] <= timestamp(utc_now()):
                raise AccessDenied("APPROVAL_EXPIRED")
            if await one(conn, "SELECT 1 FROM chat_turns WHERE conversation_id=? AND rowid>(SELECT rowid FROM chat_turns WHERE turn_id=?)", (row["conversation_id"], str(turn_id))):
                raise Conflict("IDEMPOTENCY_CONFLICT")
            proposal = json.loads(row["proposal_json"])
            if proposal["intent"] != "run_skill" or self.skills is None:
                raise AccessDenied("POLICY_DENIED")
            plan = await chat_skills.build_plan(self.skills, conn, row, proposal, value.input, require_valid=True)
            digest = digest_value(plan)
            await conn.execute("UPDATE chat_turns SET skill_plan_json=?,proposal_digest=?,revision=revision+1 WHERE turn_id=?", (canonical(plan), digest, str(turn_id)))
            await audit(conn, row["owner_id"], None, "chat.skill_previewed", utc_now())
            return {"plan": plan, "proposal_digest": digest}

    async def submit(self, principal: Principal, conversation_id: UUID | str, value: ChatSubmission) -> dict:
        # Confirmation and the next message cannot interleave task association.
        async with self.harness.operation:
            return await self._submit(principal, conversation_id, value)

    async def _submit(self, principal: Principal, conversation_id: UUID | str, value: ChatSubmission) -> dict:
        value = ChatSubmission.model_validate_json(value.model_dump_json())
        encoded = canonical(value.model_dump(mode="json"))
        async with self.database.transaction() as conn:
            conversation = await self.conversation(conn, principal, conversation_id)
            old = await one(conn, TURN_SELECT + " WHERE t.owner_id=? AND t.request_id=?", (str(principal.identity_id), str(value.request_id)))
            if old:
                if old["conversation_id"] != str(conversation_id) or canonical(ChatSubmission.model_validate_json(old["request_json"]).model_dump(mode="json")) != encoded:
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
            skills = []
            work_catalog = await self.work.prepare(conn,principal,conversation_id,value) if value.work_grant_id else []
            if value.allow_skills:
                directory = await chat_skills.catalog(self, conn, principal, value.node_id)
                if directory["skills_digest"] != value.skills_digest:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                skills = directory["skills"]
            if conversation["archived"] or conversation["revision"] != value.expected_revision:
                raise Conflict("IDEMPOTENCY_CONFLICT")
            if await one(conn, f"SELECT 1 FROM chat_turns WHERE conversation_id=? AND {ACTIVE}", (str(conversation_id),)):
                raise Conflict("BUSY")
            now = utc_now()
            recent = await one(conn, "SELECT count(*) FROM chat_turns WHERE owner_id=? AND created_at>?", (str(principal.identity_id), timestamp(now-timedelta(minutes=1))))
            total = await one(conn, "SELECT count(*),coalesce(sum(length(CAST(context_json AS BLOB))+length(CAST(response_text AS BLOB))+length(CAST(request_json AS BLOB))+coalesce(length(CAST(result_preview_json AS BLOB)),0)+coalesce(length(CAST(initial_response_text AS BLOB)),0)+coalesce(length(CAST(auto_result_json AS BLOB)),0)+length(CAST(skill_catalog_json AS BLOB))+coalesce(length(CAST(skill_plan_json AS BLOB)),0)),0) FROM chat_turns")
            count = (await one(conn, "SELECT count(*) FROM chat_turns WHERE conversation_id=?", (str(conversation_id),)))[0]
            if recent[0] >= 10 or total[0] >= 10000 or total[1] >= 268000000 or count >= 200:
                raise Conflict("BUSY")
            messages = [{"role": "user", "content": value.text}]
            refs = []
            if value.memory_ids:
                snapshot = await memory_context.selection(conn, principal.identity_id, conversation["project_id"], value.memory_ids)
                if snapshot["memory_digest"] != value.memory_digest:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                refs = snapshot["memories"]
                messages.insert(0, memory_context.message(refs))
            selected = 0
            available = 0
            if value.include_context:
                available = (await one(conn, "SELECT count(*) FROM chat_turns WHERE conversation_id=? AND status='completed'", (str(conversation_id),)))[0]
                rows = await conn.execute_fetchall("SELECT * FROM chat_turns WHERE conversation_id=? AND status='completed' ORDER BY rowid DESC LIMIT 10", (str(conversation_id),))
                for row in rows:
                    # Referenced data and model paraphrases are not silently reused
                    # under an old memory consent. Select those memories again.
                    if json.loads(row["memory_refs_json"]):
                        break
                    if row["work_grant_id"]:
                        break
                    candidate = [{"role": "user", "content": row["text"]}, {"role": "assistant", "content": row["response_text"]}, *messages]
                    if len(canonical(candidate).encode()) > (12288 if value.work_grant_id else 24576):
                        break
                    messages, selected = candidate, selected+1
            context = canonical(messages)
            turn_id = str(uuid4())
            await conn.execute("INSERT INTO chat_turns(turn_id,conversation_id,owner_id,request_id,request_json,text,context_json,context_digest,context_count,context_truncated,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?)", (
                turn_id, str(conversation_id), str(principal.identity_id), str(value.request_id), encoded, value.text, context,
                hashlib.sha256(context.encode()).hexdigest(), selected, int(available > selected), timestamp(now), timestamp(now+timedelta(minutes=5))))
            await conn.execute("UPDATE chat_turns SET skill_catalog_json=? WHERE turn_id=?", (canonical(skills), turn_id))
            await conn.execute("UPDATE chat_turns SET memory_refs_json=? WHERE turn_id=?", (canonical(refs), turn_id))
            if value.work_grant_id:
                await conn.execute("UPDATE chat_turns SET work_grant_id=?,work_catalog_json=? WHERE turn_id=?",(str(value.work_grant_id),canonical(work_catalog),turn_id))
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
        turn = await self.get(principal, turn_id)
        if turn["skill_plan"] and value.decision == "confirm":
            if value.allow_result_model:
                raise AccessDenied("RESULT_PREVIEW_REQUIRED")
            plan = turn["skill_plan"]
            run = await self.skills.start(principal, UUID(plan["version_id"]), UUID(plan["node_id"]), plan["input"],
                                          request_id=turn_id, chat_turn_id=turn_id, plan_digest=value.proposal_digest)
            return {"skill_run": run}
        async with self.harness.operation:
            async with self.database.transaction() as conn:
                await self.owner(conn,principal)
                row=await one(conn,"SELECT t.*,c.archived FROM chat_turns t JOIN conversations c USING(conversation_id) WHERE turn_id=? AND t.owner_id=?",(str(turn_id),str(principal.identity_id)))
                if row is None:raise LookupError("chat turn not found")
                if not row["proposal_digest"] or row["proposal_digest"]!=value.proposal_digest:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                if row["skill_run_id"]:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                if row["task_id"]:
                    if value.decision!="confirm":raise Conflict("IDEMPOTENCY_CONFLICT")
                    if value.allow_result_model != bool(row["auto_result_json"]):
                        raise Conflict("IDEMPOTENCY_CONFLICT")
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
                project = await one(conn, "SELECT project_id FROM conversations WHERE conversation_id=?", (row["conversation_id"],))
                request=REQUEST.validate_json(canonical({**proposal,"request_id":str(turn_id),"project_id":project["project_id"]}))
                automatic = None
                if value.allow_result_model:
                    if not row["native_tool_call_json"]:
                        raise AccessDenied("RESULT_UNAVAILABLE")
                    if not self.config.enabled or not self.config.chat_enabled or self.provider is None:
                        raise AccessDenied("MODEL_DISABLED")
                    if await one(conn, "SELECT 1 FROM chat_turns WHERE conversation_id=? AND rowid>(SELECT rowid FROM chat_turns WHERE turn_id=?)", (row["conversation_id"], str(turn_id))):
                        raise Conflict("IDEMPOTENCY_CONFLICT")
                    now = utc_now()
                    automatic = canonical({"fields": RESULT_FIELDS[proposal["intent"]], "proposal_digest": value.proposal_digest,
                        "policy_version": version, "scope_digest": scope_digest(self.harness.scope(node_id)),
                        "consented_at": timestamp(now), "expires_at": timestamp(now+timedelta(minutes=5))})
                    await audit(conn, row["owner_id"], None, "chat.result_preconsented", now)
                await conn.execute("UPDATE chat_turns SET auto_result_json=? WHERE turn_id=?", (automatic, str(turn_id)))
            # Task creation and association commit together; no model call here.
            task=await self.harness.submit(principal,node_id,request,chat_turn_id=turn_id)
            return {"task":task}

    async def preview_result(self, principal: Principal, turn_id: UUID, value: ChatResultPreview) -> dict:
        async with self.harness.operation:
            async with self.database.transaction() as conn:
                await self.owner(conn, principal)
                row = await one(conn, "SELECT * FROM chat_turns WHERE turn_id=? AND owner_id=?", (str(turn_id), str(principal.identity_id)))
                if row is None:
                    raise LookupError("chat turn not found")
                conversation = await self.conversation(conn, principal, row["conversation_id"])
                if row["phase"] != "reply" or row["status"] != "completed" or conversation["archived"]:
                    raise AccessDenied("RESULT_UNAVAILABLE")
                value = await preview(self, conn, row, conversation, value.fields)
                # Only the latest, idle round can be continued; no reordering of history.
                encoded = canonical(value)
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                await conn.execute("UPDATE chat_turns SET result_preview_json=?,result_digest=?,revision=revision+1 WHERE turn_id=?", (encoded, digest, str(turn_id)))
                await validate_preview(self, conn, await one(conn, "SELECT * FROM chat_turns WHERE turn_id=?", (str(turn_id),)))
                return public_preview(value, digest)

    async def consent_result(self, principal: Principal, turn_id: UUID, value: ChatResultConsent) -> dict:
        async with self.harness.operation:
            async with self.database.transaction() as conn:
                await self.owner(conn, principal)
                row = await one(conn, "SELECT * FROM chat_turns WHERE turn_id=? AND owner_id=?", (str(turn_id), str(principal.identity_id)))
                if row is None:
                    raise LookupError("chat turn not found")
                if not value.allow_model:
                    raise AccessDenied("MODEL_CONSENT_REQUIRED")
                if not row["result_digest"] or row["result_digest"] != value.result_digest:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                if row["phase"] == "summary":
                    return public_turn(await one(conn, TURN_SELECT+" WHERE t.turn_id=?", (str(turn_id),)))
                conversation = await self.conversation(conn, principal, row["conversation_id"])
                if conversation["archived"] or conversation["revision"] != value.expected_revision or row["status"] != "completed":
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                if self.failed or self.harness.failed or (await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0]:
                    raise AccessDenied("BUSY")
                if not self.config.enabled or not self.config.chat_enabled or self.provider is None:
                    raise AccessDenied("MODEL_DISABLED")
                snapshot = await validate_preview(self, conn, row)
                if snapshot["conversation_revision"] != value.expected_revision:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                await queue_summary(conn, row, timestamp(utc_now()))
                await audit(conn, row["owner_id"], row["task_id"], "chat.result_consented", utc_now())
                return public_turn(await one(conn, TURN_SELECT+" WHERE t.turn_id=?", (str(turn_id),)))

    async def cancel(self, principal: Principal, turn_id: UUID | str) -> dict:
        async with self.harness.operation:
            async with self.database.transaction() as conn:
                await self.owner(conn, principal)
                row = await one(conn, "SELECT * FROM chat_turns WHERE turn_id=? AND owner_id=?", (str(turn_id), str(principal.identity_id)))
                if row is None:
                    raise LookupError("turn not found")
                if row["work_grant_id"] and (row["work_status"] or row["status"] in {"queued","generating"}):
                    await self.work.abort(conn,str(turn_id),"WORK_USER_STOPPED",utc_now())
                if row["status"] in {"queued", "generating"}:
                    await conn.execute("UPDATE chat_turns SET status=?,revision=revision+1 WHERE turn_id=?", ("stopped" if row["status"] == "queued" else "stopping", str(turn_id)))
                    await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id=?", (row["conversation_id"],))
                    await audit(conn, str(principal.identity_id), None, "chat.stop_requested", utc_now())
                if row["auto_result_status"] == "waiting":
                    await conn.execute("UPDATE chat_turns SET auto_result_status='stopped',revision=revision+1 WHERE turn_id=?", (str(turn_id),))
                    await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id=?", (row["conversation_id"],))
                    await audit(conn, str(principal.identity_id), row["task_id"], "chat.auto_result_stopped", utc_now())
            if self.active and self.active[0] == str(turn_id):
                self.active[1].cancel()
        return await self.get(principal, turn_id)

    async def recover(self) -> None:
        async with self.database.transaction() as conn:
            await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id IN (SELECT conversation_id FROM chat_turns WHERE status IN ('generating','stopping'))")
            await conn.execute("UPDATE model_calls SET cost_status='unknown',outcome='unknown',finished_at=? WHERE (chat_turn_id IS NOT NULL OR work_root_turn_id IS NOT NULL) AND outcome='started'", (timestamp(utc_now()),))
            await conn.execute("UPDATE chat_turns SET status=CASE WHEN status='stopping' THEN 'stopped' ELSE 'interrupted' END,revision=revision+1,error_code='MODEL_OUTCOME_UNKNOWN' WHERE status IN ('generating','stopping')")
            rows=await conn.execute_fetchall("SELECT turn_id FROM chat_turns WHERE work_status!=''")
            for row in rows:
                await self.work.abort(conn,row[0],"CORE_RESTARTED",utc_now())

    async def delta(self, turn_id: str, text: str) -> None:
        async with self.database.transaction() as conn:
            cursor = await conn.execute("UPDATE chat_turns SET response_text=?,revision=revision+1 WHERE turn_id=? AND status='generating'", (text, turn_id))
            if cursor.rowcount != 1:
                raise asyncio.CancelledError()

    async def claim(self):
        if self.failed or self.harness.failed:
            return None
        await self.work.advance()
        await self.queue_automatic_result()
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
                summary = row["phase"] == "summary"
                reservation = self.config.task_cost_microunits
                messages = json.loads(row["context_json"])
                error = None
                try:
                    await memory_context.validate_turn(conn, row)
                    if row["work_grant_id"]:
                        reservation,messages = await self.work.model_start(conn,row,now)
                except AccessDenied as exc:
                    error = exc.code
                if summary:
                    calls = await conn.execute_fetchall("SELECT * FROM model_calls WHERE chat_turn_id=?", (row["turn_id"],))
                    if len(calls) != 1 or calls[0]["cost_status"] != "calculated" or calls[0]["outcome"] != "completed":
                        error = "BUDGET_EXCEEDED"
                    else:
                        reservation = min(reservation, calls[0]["reserved_microunits"])-calls[0]["cost_microunits"]
                        worst = (32768*self.config.input_price+self.config.chat_max_output_tokens*self.config.output_price+999999)//1000000
                        if reservation < worst:
                            error = "BUDGET_EXCEEDED"
                    try:
                        snapshot = await validate_preview(self, conn, row)
                        messages = snapshot["messages"]
                    except (AccessDenied, ValueError) as exc:
                        error = exc.code if isinstance(exc, AccessDenied) else "IDEMPOTENCY_CONFLICT" if isinstance(exc, Conflict) else "RESULT_UNAVAILABLE"
                    if (await one(conn, "SELECT archived FROM conversations WHERE conversation_id=?", (row["conversation_id"],)))[0]:
                        error = "APPROVAL_EXPIRED"
                owner = await one(conn, "SELECT 1 FROM identities WHERE identity_id=? AND role='owner' AND revoked_at IS NULL", (row["owner_id"],))
                error = error or ("AUTH_REQUIRED" if not owner else "MODEL_DISABLED" if not self.config.chat_enabled or self.provider is None else await budget_error(conn, self.config, now, reservation=reservation))
                if not summary and row["expires_at"] <= timestamp(now):
                    error = "DEADLINE_EXCEEDED"
                if error:
                    if row["work_grant_id"]:
                        await self.work.abort(conn,row["turn_id"],error,now)
                    await conn.execute("UPDATE chat_turns SET status=?,error_code=?,revision=revision+1 WHERE turn_id=?", ("expired" if error == "DEADLINE_EXCEEDED" else "failed", error, row["turn_id"]))
                    await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id=?", (row["conversation_id"],))
                    return None
                call_id = str(uuid4())
                await conn.execute("INSERT INTO model_calls(call_id,provider,model,cost_status,created_at,owner_id,reserved_microunits,pricing_json,outcome,chat_turn_id,chat_stage,task_id) VALUES(?,'deepseek',?,'reserved',?,?,?,?, 'started',?,?,?)", (
                    call_id, MODEL, timestamp(now), row["owner_id"], reservation,
                    canonical({"input": self.config.input_price, "output": self.config.output_price, "currency": "USD", "valid_until": self.config.price_valid_until.isoformat()}), None if row["work_grant_id"] else row["turn_id"], int(summary), row["task_id"]))
                if row["work_grant_id"]:
                    await conn.execute("UPDATE model_calls SET work_root_turn_id=?,work_round=? WHERE call_id=?",(row["turn_id"],row["work_round"],call_id))
                await conn.execute("UPDATE chat_turns SET status='generating',revision=revision+1 WHERE turn_id=?", (row["turn_id"],))
                if row["skill_run_id"]:
                    await conn.execute("UPDATE model_calls SET skill_run_id=? WHERE call_id=?", (row["skill_run_id"], call_id))
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
            options = {"generate_title": not summary and bool(row["generate_title"]), "allow_tools": not summary and request.get("allow_tools",False)}
            if not summary and request.get("allow_skills"):
                options["skill_catalog"] = json.loads(row["skill_catalog_json"])
            if summary:
                options["final_result"] = True
            if row["work_grant_id"]:
                options = {"generate_title":row["work_round"] == 0 and bool(row["generate_title"]),"allow_tools":row["work_round"] < 4,"work_catalog":json.loads(row["work_catalog_json"]) if row["work_round"] < 4 else []}
            task = asyncio.create_task(self.provider.invoke(messages, receive, **options))
            self.active = row["turn_id"], task
            return row, call_id, task, lambda: accumulated

    async def queue_automatic_result(self) -> None:
        # Idle polling is read-only; task execution/recovery remains in Harness.
        async with self.database.transaction(write=False) as conn:
            rows = await conn.execute_fetchall("SELECT t.*,c.archived,k.status AS tool_status FROM chat_turns t JOIN conversations c USING(conversation_id) JOIN tasks k ON k.task_id=t.task_id WHERE auto_result_status='waiting' ORDER BY t.rowid LIMIT 100")
        candidate = next((row for row in rows if row["archived"] or row["tool_status"] in {"succeeded", "failed", "cancelled", "timed_out"}
                          or json.loads(row["auto_result_json"])["expires_at"] <= timestamp(utc_now())), None)
        if candidate is None:
            return
        async with self.harness.operation:
            async with self.database.transaction() as conn:
                row = await one(conn, "SELECT * FROM chat_turns WHERE turn_id=?", (candidate["turn_id"],))
                if row["auto_result_status"] != "waiting" or row["phase"] != "reply":
                    return
                consent = json.loads(row["auto_result_json"])
                conversation = await one(conn, "SELECT * FROM conversations WHERE conversation_id=?", (row["conversation_id"],))
                now = utc_now()
                if not conversation["archived"] and consent["expires_at"] > timestamp(now) and (await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0]:
                    return
                error = None
                try:
                    if conversation["archived"] or consent["expires_at"] <= timestamp(now):
                        raise AccessDenied("APPROVAL_EXPIRED")
                    snapshot = await preview(self, conn, row, conversation, consent["fields"])
                    if (consent["proposal_digest"] != row["proposal_digest"] or any(consent[key] != snapshot[key] for key in ("policy_version", "scope_digest"))):
                        raise AccessDenied("POLICY_DENIED")
                    snapshot["expires_at"] = min(snapshot["expires_at"], consent["expires_at"])
                    encoded = canonical(snapshot)
                    digest = hashlib.sha256(encoded.encode()).hexdigest()
                    await conn.execute("UPDATE chat_turns SET result_preview_json=?,result_digest=? WHERE turn_id=?", (encoded, digest, row["turn_id"]))
                    await validate_preview(self, conn, await one(conn, "SELECT * FROM chat_turns WHERE turn_id=?", (row["turn_id"],)))
                except (AccessDenied, ValueError) as exc:
                    error = exc.code if isinstance(exc, AccessDenied) else "RESULT_UNAVAILABLE"
                if error:
                    await conn.execute("UPDATE chat_turns SET auto_result_status='unavailable',error_code=?,revision=revision+1 WHERE turn_id=?", (error, row["turn_id"]))
                    await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id=?", (row["conversation_id"],))
                    await audit(conn, row["owner_id"], row["task_id"], "chat.auto_result_unavailable", now)
                elif not (await one(conn, "SELECT paused FROM system_state WHERE singleton=1"))[0]:
                    await queue_summary(conn, row, consent["consented_at"])
                    await audit(conn, row["owner_id"], row["task_id"], "chat.result_auto_queued", now)

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
            reserved = (await one(conn, "SELECT reserved_microunits FROM model_calls WHERE call_id=?", (call_id,)))[0]
            if cost is not None and cost > reserved:
                cost, reply, status, error = None, None, "failed", "MODEL_USAGE_INVALID"
            current = await one(conn, "SELECT status FROM chat_turns WHERE turn_id=?", (row["turn_id"],))
            if current[0] == "stopping":
                status, error = "stopped", None
            elif interrupted:
                status, error = "interrupted", "MODEL_OUTCOME_UNKNOWN"
            proposal=None
            plan=None
            if status=="completed" and reply is not None and reply.proposal is not None:
                try:
                    if row["work_grant_id"]:
                        await self.work.proposed(conn,row,reply,now)
                    else:
                        proposal=CHAT_PROPOSAL.validate_json(canonical(reply.proposal)).model_dump(mode="json")
                    if not row["work_grant_id"] and (row["phase"] == "summary" or not json.loads(row["request_json"]).get("allow_tools")):
                        raise AccessDenied("POLICY_DENIED")
                    version=(await one(conn,"SELECT version FROM policy_state WHERE singleton=1"))[0]
                    if proposal and proposal["intent"] == "run_skill":
                        if self.skills is None:
                            raise AccessDenied("POLICY_DENIED")
                        plan = await chat_skills.build_plan(self.skills, conn, row, proposal, proposal["arguments"]["input"])
                        digest = digest_value(plan)
                    elif proposal:
                        digest=self.action_digest(row,proposal,version)
                    if proposal and reply.native_tool_call is not None:
                        checked_native(reply.native_tool_call, proposal)
                except (ValidationError,AccessDenied,ValueError,TypeError,KeyError):
                    proposal=None
                    status,error="failed","POLICY_DENIED"
            if row["work_grant_id"] and status not in {"completed"}:
                await self.work.abort(conn,row["turn_id"],error or "WORK_STOPPED",now)
            await conn.execute("UPDATE model_calls SET cost_microunits=?,cost_status=?,input_tokens=?,output_tokens=?,finished_at=?,outcome=?,model=? WHERE call_id=? AND outcome='started'", (
                cost, "calculated" if cost is not None else "unknown", reply.input_tokens if reply else None, reply.output_tokens if reply else None,
                timestamp(now), "completed" if reply else "unknown", reply.model[:200] if reply else MODEL, call_id))
            if current[0] in {"generating", "stopping"}:
                await conn.execute("UPDATE chat_turns SET response_text=?,status=?,error_code=?,revision=revision+1 WHERE turn_id=?", (reply.content if reply else partial, status, error, row["turn_id"]))
                await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id=?", (row["conversation_id"],))
                if status=="completed" and reply is not None:
                    if row["phase"] == "reply" and row["generate_title"] and reply.title:
                        try:
                            title=ChatTitle(title=reply.title).title.strip()
                        except ValidationError:
                            title=None
                        if title:
                            await conn.execute("UPDATE conversations SET title=?,auto_title=0 WHERE conversation_id=? AND auto_title=1",(title,row["conversation_id"]))
                    if proposal is not None:
                        await conn.execute("UPDATE chat_turns SET proposal_json=?,proposal_digest=?,proposal_policy_version=?,action_status='proposed',native_tool_call_json=? WHERE turn_id=?",(canonical(proposal),digest,version,canonical(reply.native_tool_call) if reply.native_tool_call else None,row["turn_id"]))
                        if plan is not None:
                            await conn.execute("UPDATE chat_turns SET skill_plan_json=? WHERE turn_id=?", (canonical(plan), row["turn_id"]))
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
