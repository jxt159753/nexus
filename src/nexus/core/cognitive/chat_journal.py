"""Owner-visible exchanges, explicit corrections and reconciled work recovery."""

import hashlib
import json
from nexus.core.cognitive import node_routing
from dataclasses import asdict
from uuid import UUID, uuid4

from nexus.contracts.extensions import canonical, digest_value
from nexus.core.models.chat import request_body
from nexus.core.models.config import CHAT_MAX_TEXT_BYTES
from nexus.core.memory import context as memory_context
from nexus.core.security import work as permissions
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict

QUEUE_SQL = """
SELECT t.*,(c.auto_title=1 AND NOT EXISTS(
    SELECT 1 FROM chat_turns older WHERE older.conversation_id=t.conversation_id AND older.rowid<t.rowid
)) AS generate_title FROM chat_turns t JOIN conversations c USING(conversation_id)
WHERE t.status='queued' AND t.recovery_required=0 AND NOT EXISTS(
    SELECT 1 FROM chat_turns prior WHERE prior.conversation_id=t.conversation_id AND prior.rowid<t.rowid AND (
        prior.status IN ('queued','generating','stopping') OR prior.work_status!=''
        OR prior.recovery_required=1 OR prior.auto_result_status='waiting'
        OR EXISTS(SELECT 1 FROM chat_work_steps w JOIN chat_work_tasks k USING(step_id)
            JOIN task_attempts a USING(task_id) WHERE w.turn_id=prior.turn_id AND a.execution_stopped=0)
        OR EXISTS(SELECT 1 FROM chat_work_steps w JOIN skill_run_steps k ON k.run_id=w.skill_run_id
            JOIN task_attempts a USING(task_id) WHERE w.turn_id=prior.turn_id AND a.execution_stopped=0)
        OR EXISTS(SELECT 1 FROM chat_work_steps w JOIN skill_runs r ON r.run_id=w.skill_run_id
            WHERE w.turn_id=prior.turn_id AND r.status IN ('running','cancelling'))
        OR EXISTS(SELECT 1 FROM chat_work_steps w JOIN extension_tool_runs r
            ON r.work_step_id=w.step_id OR r.skill_run_id=w.skill_run_id
            LEFT JOIN sandbox_executions e ON e.execution_id=r.tool_run_id
            WHERE w.turn_id=prior.turn_id AND (r.status IN ('dispatching','unknown') OR e.status IN ('prepared','sent','unknown')))
    )
) ORDER BY t.rowid LIMIT 1
"""


async def capacity(conn):
    size = await one(conn,"SELECT coalesce(sum(length(CAST(request_body AS BLOB))+length(CAST(partial_text AS BLOB))+coalesce(length(CAST(response_json AS BLOB)),0)),0) FROM chat_exchanges")
    compact = await one(conn,"SELECT coalesce(sum(length(CAST(source_messages_json AS BLOB))+length(CAST(summary_text AS BLOB))),0) FROM chat_compactions")
    # JSON escaping can expand response text sixfold; partial_text is separate.
    return size[0]+compact[0]+7*CHAT_MAX_TEXT_BYTES+262144 <= 268435456


async def begin(conn, config, row, call_id, messages, options, now):
    # This is the identical serializer used by the HTTP provider; no headers/keys.
    body = request_body(config,messages,**options).decode("utf-8")
    await begin_payload(conn,row["turn_id"],call_id,body,now)
    await conn.execute("UPDATE chat_controls SET applied_call_id=? WHERE turn_id=? AND kind='steer' AND applied_call_id IS NULL",(call_id,row["turn_id"]))


async def begin_payload(conn, turn_id, call_id, body, now):
    await conn.execute("INSERT INTO chat_exchanges(call_id,turn_id,request_body,request_sha256,created_at) VALUES(?,?,?,?,?)",(call_id,turn_id,body,hashlib.sha256(body.encode()).hexdigest(),timestamp(now)))


async def finish(conn, call_id, reply, partial, status, now, *, failure=None):
    response = asdict(reply) if reply else {"content": partial, "complete": False, **failure} if failure else None
    outcome = "failed" if reply and getattr(reply, "rejected_calls", ()) else status
    await conn.execute("UPDATE chat_exchanges SET response_json=?,partial_text=?,outcome=?,finished_at=? WHERE call_id=? AND outcome='started'",(canonical(response) if response is not None else None,partial,outcome if outcome in {"completed","failed","stopped","interrupted"} else "unknown",timestamp(now),call_id))


async def corrections(conn, turn_id):
    rows=await conn.execute_fetchall("SELECT control_id,text FROM chat_controls WHERE turn_id=? AND kind='steer' AND applied_call_id IS NULL ORDER BY rowid",(turn_id,))
    return [{"role":"user","content":"[用户中途纠正；从下一步生效，不扩大已有权限]\n"+row["text"]} for row in rows]


class ChatJournal:
    def __init__(self, chat):
        self.chat=chat

    async def detail(self, principal, turn_id):
        async with self.chat.database.transaction(write=False) as conn:
            await self.owned(conn,principal,turn_id)
            exchanges=await conn.execute_fetchall("SELECT e.*,m.provider,m.model,m.cost_status,m.cost_microunits FROM chat_exchanges e JOIN model_calls m USING(call_id) WHERE e.turn_id=? ORDER BY e.rowid",(str(turn_id),))
            from nexus.core.storage.archive import hydrate
            exchanges=[await hydrate(conn,'chat_exchanges',row) for row in exchanges]
            if any(hashlib.sha256(row["request_body"].encode()).hexdigest()!=row["request_sha256"] for row in exchanges):
                raise AccessDenied("RESULT_INVALID")
            controls=await conn.execute_fetchall("SELECT control_id,kind,text,applied_call_id,created_at FROM chat_controls WHERE turn_id=? ORDER BY rowid",(str(turn_id),))
            compactions=await conn.execute_fetchall("SELECT * FROM chat_compactions WHERE turn_id=?",(str(turn_id),))
            compactions=[await hydrate(conn,'chat_compactions',row) for row in compactions]
            return {"exchanges":[dict(row) for row in exchanges],"controls":[dict(row) for row in controls],"compactions":[dict(row) for row in compactions]}

    async def owned(self, conn, principal, turn_id):
        await self.chat.owner(conn,principal)
        row=await one(conn,"SELECT * FROM chat_turns WHERE turn_id=? AND owner_id=?",(str(turn_id),str(principal.identity_id)))
        if row is None:
            raise LookupError("turn not found")
        return row

    async def steer(self, principal, turn_id, value):
        async with self.chat.harness.operation,self.chat.database.transaction() as conn:
            row=await self.owned(conn,principal,turn_id)
            encoded=canonical(value.model_dump(mode="json"))
            old=await one(conn,"SELECT * FROM chat_controls WHERE request_id=?",(str(value.request_id),))
            if old:
                if old["turn_id"]!=str(turn_id) or old["request_json"]!=encoded:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return
            if (not row["work_grant_id"] or row["recovery_required"] or not(row["work_status"] or row["status"] in {"queued","generating"}) or row["status"] in {"stopping","stopped","failed","interrupted"}):
                raise AccessDenied("ALREADY_FINISHED")
            await node_routing.validate_turn(self.chat,conn,row,utc_now())
            if (await one(conn,"SELECT count(*) FROM chat_controls WHERE turn_id=? AND kind='steer'",(str(turn_id),)))[0]>=8:
                raise AccessDenied("BUSY")
            await conn.execute("INSERT INTO chat_controls(control_id,turn_id,request_id,request_json,kind,text,created_at) VALUES(?,?,?,?,'steer',?,?)",(str(uuid4()),str(turn_id),str(value.request_id),encoded,value.text,timestamp(utc_now())))
            await audit(conn,str(principal.identity_id),None,"chat.steered",utc_now())

    async def resume(self, principal, turn_id, value):
        async with self.chat.harness.operation,self.chat.database.transaction() as conn:
            row=await self.owned(conn,principal,turn_id)
            encoded=canonical(value.model_dump(mode="json"))
            old=await one(conn,"SELECT * FROM chat_controls WHERE request_id=?",(str(value.request_id),))
            if old:
                if old["turn_id"]!=str(turn_id) or old["request_json"]!=encoded:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return
            if not row["recovery_required"] or row["expires_at"]<=timestamp(utc_now()):
                raise AccessDenied("APPROVAL_EXPIRED")
            await node_routing.validate_turn(self.chat,conn,row,utc_now())
            if await one(conn,"SELECT 1 FROM model_calls WHERE work_root_turn_id=? AND (outcome!='completed' OR cost_status!='calculated')",(str(turn_id),)):
                raise AccessDenied("EXECUTION_UNKNOWN")
            tasks=await conn.execute_fetchall("""SELECT t.* FROM tasks t WHERE t.task_id IN (
                SELECT k.task_id FROM chat_work_tasks k JOIN chat_work_steps s USING(step_id) WHERE s.turn_id=?
                UNION SELECT k.task_id FROM skill_run_steps k JOIN chat_work_steps s ON s.skill_run_id=k.run_id WHERE s.turn_id=?
            )""",(str(turn_id),str(turn_id)))
            for task in tasks:
                if task["execution_outcome"]=="unknown":
                    raise AccessDenied("EXECUTION_UNKNOWN")
                if task["status"] not in {"succeeded","failed","cancelled","timed_out"} and not(task["status"] in {"queued","ready","awaiting_approval"} and task["execution_outcome"]=="not_started" and not task["current_attempt_id"]):
                    raise AccessDenied("BUSY")
            skills=await conn.execute_fetchall("SELECT r.* FROM skill_runs r JOIN chat_work_steps s ON s.skill_run_id=r.run_id WHERE s.turn_id=?",(str(turn_id),))
            for skill in skills:
                if skill["status"]=="cancelling" or skill["cancel_requested"]:
                    raise AccessDenied("ALREADY_FINISHED")
                if await one(conn,"SELECT 1 FROM skill_run_steps WHERE run_id=? AND status='unknown'",(skill["run_id"],)):
                    raise AccessDenied("EXECUTION_UNKNOWN")
            if await one(conn,"SELECT 1 FROM extension_tool_runs r JOIN chat_work_steps s ON r.work_step_id=s.step_id OR r.skill_run_id=s.skill_run_id LEFT JOIN sandbox_executions e ON e.execution_id=r.tool_run_id WHERE s.turn_id=? AND (r.status IN ('dispatching','unknown') OR e.status IN ('prepared','sent','unknown'))",(str(turn_id),)):
                raise AccessDenied("EXECUTION_UNKNOWN")
            await conn.execute("UPDATE chat_turns SET recovery_required=0,revision=revision+1 WHERE turn_id=?",(str(turn_id),))
            await conn.execute("INSERT INTO chat_controls(control_id,turn_id,request_id,request_json,kind,created_at) VALUES(?,?,?,?,'resume',?)",(str(uuid4()),str(turn_id),str(value.request_id),encoded,timestamp(utc_now())))
            await audit(conn,str(principal.identity_id),None,"chat.recovery_resumed",utc_now())

    async def references(self, conn, principal, project_id, turn_ids):
        records=[]
        for identifier in turn_ids:
            row=await self.owned(conn,principal,identifier)
            from nexus.core.storage.archive import hydrate
            row=await hydrate(conn,'chat_turns',row)
            source=await one(conn,"SELECT project_id FROM conversations WHERE conversation_id=?",(row["conversation_id"],))
            if json.loads(row["references_json"]) or source[0]!=project_id or row["status"]!="completed" or row["work_status"] or row["recovery_required"]:
                raise AccessDenied("POLICY_DENIED")
            # Old memory consent never becomes implicit permission to reuse a paraphrase.
            await memory_context.validate_turn(conn,row)
            await self.chat.attachments.validate_turn(conn,row)
            records.append({"turn_id":row["turn_id"],"conversation_id":row["conversation_id"],"revision":row["revision"],"text":row["text"],"response":row["response_text"]})
        if len(canonical(records).encode())>8192:
            raise AccessDenied("MODEL_INPUT_LIMIT")
        return {"references":records,"references_digest":digest_value(records)}

    async def preview_references(self, principal, conversation_id, value):
        async with self.chat.database.transaction(write=False) as conn:
            conversation=await self.chat.conversation(conn,principal,conversation_id)
            return await self.references(conn,principal,conversation["project_id"],value.turn_ids)
