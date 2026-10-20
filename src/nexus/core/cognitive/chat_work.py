"""Bounded chat work coordination; all effects use the existing executors."""

import json
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from nexus.contracts.extensions import canonical
from nexus.core.cognitive import work_catalog
from nexus.core.security import work as permissions
from nexus.core.security.service import scope_digest
from nexus.core.security.types import AccessDenied, Principal
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import REQUEST, Conflict, TERMINAL, load_task
from nexus.core.storage.control import cancel_task


def request_for(step, role, project_id, command_digests, preview=None):
    proposal = json.loads(step["proposal_json"])
    intent, arguments = proposal["intent"], dict(proposal["arguments"])
    if intent == "run_command":
        arguments["template_digest"] = command_digests.get(arguments["command_id"], "")
    if intent == "write_file":
        if role == "preview":
            intent = "preview_file_write"
        else:
            if not preview:
                raise AccessDenied("WRITE_PREVIEW_REQUIRED")
            arguments.update(expected_sha256=preview["previous_sha256"], preview_task_id=step["preview_task_id"],preview_digest=preview["preview_digest"])
    return REQUEST.validate_json(canonical({"request_id":str(uuid5(NAMESPACE_URL,f"nexus-work:{step['step_id']}:{role}")),"project_id":project_id,"intent":intent,"arguments":arguments}))


async def validate_task(conn, step_id, role, owner_id, node_id, request, now):
    step, grant = await permissions.allowed_step(conn,step_id,now)
    if role not in {"preview","execute"} or grant["owner_id"] != str(owner_id) or grant["node_id"] != str(node_id):
        raise AccessDenied("POLICY_DENIED")
    project = await one(conn,"SELECT c.project_id FROM conversations c JOIN chat_turns t USING(conversation_id) WHERE t.turn_id=?",(step["turn_id"],))
    proposal = json.loads(step["proposal_json"])
    arguments = proposal["arguments"]
    if request.intent == "preview_file_write" and proposal["intent"] == "write_file" and role == "preview":
        pass
    elif request.intent != proposal["intent"] or role != "execute":
        raise AccessDenied("POLICY_DENIED")
    actual = request.arguments.model_dump(mode="json")
    if any(actual.get(key) != value for key,value in arguments.items()) or project[0] != (str(request.project_id) if request.project_id else None):
        raise AccessDenied("POLICY_DENIED")
    if request.intent == "write_file" and (not grant["auto_files"] and step["permission_decision"] != "approve" or str(request.arguments.preview_task_id) != step["preview_task_id"]):
        raise AccessDenied("WORK_CONFIRMATION_REQUIRED")
    if request.intent == "run_command" and grant["profile"] != "standard" and step["permission_decision"] != "approve":
        raise AccessDenied("WORK_CONFIRMATION_REQUIRED")


async def validate_extension(conn, step_id, owner_id, version_id, node_id, input_value, now, kind):
    step, grant = await permissions.allowed_step(conn,step_id,now)
    proposal = json.loads(step["proposal_json"])
    if grant["owner_id"] != str(owner_id) or proposal != {"intent":"run_"+kind,"arguments":{"version_id":str(version_id),"input":input_value}} or kind == "skill" and grant["node_id"] != str(node_id):
        raise AccessDenied("POLICY_DENIED")
    return step


async def cancel_children(conn, turn_id, now):
    rows = await conn.execute_fetchall("SELECT k.task_id FROM chat_work_steps w JOIN chat_work_tasks k USING(step_id) WHERE w.turn_id=?",(turn_id,))
    for row in rows:
        task = await load_task(conn,UUID(row[0]))
        await cancel_task(conn,task,task.owner_id,now)
    await conn.execute("UPDATE skill_runs SET status='cancelling',cancel_requested=1,revision=revision+1 WHERE run_id IN (SELECT skill_run_id FROM chat_work_steps WHERE turn_id=?) AND status='running'",(turn_id,))


class WorkService:
    def __init__(self, chat):
        self.chat = chat
        self.database = chat.database
        from nexus.core.cognitive.web_work import WebWork
        self.web = WebWork(self)

    async def grant(self, principal, conversation_id, value):
        async with self.chat.harness.operation, self.database.transaction() as conn:
            now = utc_now()
            conversation = await self.chat.conversation(conn,principal,conversation_id)
            encoded = canonical(value.model_dump(mode="json"))
            existing = await one(conn,"SELECT * FROM chat_work_grants WHERE owner_id=? AND request_id=?",(str(principal.identity_id),str(value.request_id)))
            if existing:
                if existing["request_json"] != encoded or existing["conversation_id"] != str(conversation_id):
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return self.public_grant(existing)
            if conversation["archived"] or await one(conn,"SELECT 1 FROM chat_turns WHERE conversation_id=? AND (status IN ('queued','generating','stopping') OR work_status!='')",(str(conversation_id),)):
                raise Conflict("BUSY")
            directory = await work_catalog.catalog(self.chat,conn,principal,value.node_id)
            if (await one(conn,"SELECT count(*) FROM chat_work_grants"))[0] >= 10000:
                raise Conflict("BUSY")
            await conn.execute("UPDATE chat_work_grants SET revoked_at=? WHERE conversation_id=? AND revoked_at IS NULL",(timestamp(now),str(conversation_id)))
            identifier = str(uuid4())
            await conn.execute("INSERT INTO chat_work_grants(grant_id,conversation_id,owner_id,node_id,request_id,request_json,profile,scope_digest,policy_version,max_operations,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(
                identifier,str(conversation_id),str(principal.identity_id),str(value.node_id),str(value.request_id),encoded,value.profile,
                scope_digest(self.chat.harness.scope(value.node_id)),(await one(conn,"SELECT version FROM policy_state WHERE singleton=1"))[0],value.max_operations,timestamp(now),timestamp(now+timedelta(minutes=value.duration_minutes))))
            await audit(conn,str(principal.identity_id),None,"chat.work_granted",now)
            return self.public_grant(await one(conn,"SELECT * FROM chat_work_grants WHERE grant_id=?",(identifier,)))

    @staticmethod
    def public_grant(row):
        return {key:row[key] for key in ("grant_id","conversation_id","node_id","profile","max_operations","used_operations","expires_at","revoked_at")}

    async def revoke(self,principal,conversation_id):
        async with self.chat.harness.operation, self.database.transaction() as conn:
            await self.chat.conversation(conn,principal,conversation_id)
            now = utc_now()
            await conn.execute("UPDATE chat_work_grants SET revoked_at=? WHERE conversation_id=? AND revoked_at IS NULL",(timestamp(now),str(conversation_id)))
            rows = await conn.execute_fetchall("SELECT turn_id FROM chat_turns WHERE conversation_id=? AND (work_status!='' OR work_grant_id IS NOT NULL AND status IN ('queued','generating'))",(str(conversation_id),))
            for row in rows:
                await self.abort(conn,row[0],"WORK_PERMISSION_REVOKED",now)
            await audit(conn,str(principal.identity_id),None,"chat.work_revoked",now)

    async def describe(self,conn,conversation_id):
        row = await one(conn,"SELECT * FROM chat_work_grants WHERE conversation_id=? AND revoked_at IS NULL ORDER BY rowid DESC LIMIT 1",(str(conversation_id),))
        if row is None:
            return None
        value=self.public_grant(row)
        try:
            await permissions.valid_grant(conn,row["grant_id"],utc_now(),scope=self.chat.harness.scope(UUID(row["node_id"])))
            value["valid"]=True
        except AccessDenied:
            value["valid"]=False
        return value

    async def steps(self,conn,turn_id):
        rows = await conn.execute_fetchall("SELECT * FROM chat_work_steps WHERE turn_id=? ORDER BY step_index",(str(turn_id),))
        output=[]
        for row in rows:
            value=dict(row)
            value["proposal"] = json.loads(value.pop("proposal_json"))
            value["result"] = json.loads(value.pop("result_json")) if row["result_json"] else None
            value.pop("native_json")
            output.append(value)
        return output

    async def prepare(self,conn,principal,conversation_id,value):
        directory = await work_catalog.catalog(self.chat,conn,principal,value.node_id)
        if directory["work_digest"] != value.work_digest:
            raise Conflict("IDEMPOTENCY_CONFLICT")
        if value.workspace_work:
            # Existing configured workspace + persisted identity grants are the
            # authorization. This internal scope is not an extra user approval.
            if not {"read_file","find_files","write_file","web_search","web_fetch"}.intersection(item["name"] for item in directory["catalog"]):
                raise AccessDenied("POLICY_DENIED")
            now=utc_now()
            if (await one(conn,"SELECT count(*) FROM chat_work_grants"))[0]>=10000:
                raise Conflict("BUSY")
            identifier=str(uuid4())
            await conn.execute("INSERT INTO chat_work_grants(grant_id,conversation_id,owner_id,node_id,request_id,request_json,profile,scope_digest,policy_version,max_operations,created_at,expires_at,auto_files) VALUES(?,?,?,?,?,?,'standard',?,?,20,?,?,1)",(
                identifier,str(conversation_id),str(principal.identity_id),str(value.node_id),str(value.request_id),canonical({"source":"configured_workspace","request_id":str(value.request_id)}),scope_digest(self.chat.harness.scope(value.node_id)),(await one(conn,"SELECT version FROM policy_state WHERE singleton=1"))[0],timestamp(now),timestamp(now+timedelta(minutes=5))))
            await audit(conn,str(principal.identity_id),None,"chat.workspace_scope",now)
            return directory["catalog"],identifier
        grant = await permissions.valid_grant(conn,value.work_grant_id,utc_now(),owner_id=principal.identity_id,node_id=value.node_id,scope=self.chat.harness.scope(value.node_id))
        if grant["conversation_id"] != str(conversation_id) or grant["used_operations"] >= grant["max_operations"]:
            raise AccessDenied("WORK_PERMISSION_EXPIRED")
        return directory["catalog"],str(value.work_grant_id)

    async def model_start(self,conn,row,now):
        grant = await permissions.valid_grant(conn,row["work_grant_id"],now,owner_id=row["owner_id"],scope=self.chat.harness.scope(UUID(json.loads(row["request_json"])["node_id"])))
        if row["work_round"] > 4:
            raise AccessDenied("WORK_OPERATION_LIMIT")
        calls = await conn.execute_fetchall("SELECT * FROM model_calls WHERE work_root_turn_id=?",(row["turn_id"],))
        if sum(call["web_step_id"] is None for call in calls) != row["work_round"] or any(call["cost_status"] != "calculated" or call["outcome"] != "completed" for call in calls):
            raise AccessDenied("BUDGET_EXCEEDED")
        remaining = self.chat.config.task_cost_microunits-sum(call["cost_microunits"] for call in calls)
        worst=(32768*self.chat.config.input_price+self.chat.config.chat_max_output_tokens*self.chat.config.output_price+999999)//1000000
        if remaining < worst:
            raise AccessDenied("BUDGET_EXCEEDED")
        return remaining, json.loads(row["work_messages_json"] or row["context_json"])

    async def proposed(self,conn,row,reply,now):
        count=(await one(conn,"SELECT count(*) FROM chat_work_steps WHERE turn_id=?",(row["turn_id"],)))[0]
        calls=reply.work_calls or ((reply.proposal,reply.native_tool_call),)
        if row["work_round"] >= 4 or not 1<=len(calls)<=4-count:
            raise AccessDenied("WORK_STEP_LIMIT")
        entries=json.loads(row["work_catalog_json"])
        checked=[(work_catalog.checked(entries,proposal,native),native) for proposal,native in calls]
        await permissions.valid_grant(conn,row["work_grant_id"],now,owner_id=row["owner_id"],scope=self.chat.harness.scope(UUID(json.loads(row["request_json"])["node_id"])))
        messages=json.loads(row["work_messages_json"] or row["context_json"])
        identifiers=[native["id"] for _,native in checked]
        old_ids={call["id"] for message in messages for call in message.get("tool_calls",[])}
        if len(set(identifiers))!=len(identifiers) or old_ids.intersection(identifiers):
            raise AccessDenied("MODEL_RESPONSE_INVALID")
        for index,(proposal,native) in enumerate(checked,1):
            await conn.execute("INSERT INTO chat_work_steps(step_id,turn_id,step_index,proposal_json,native_json,status,created_at) VALUES(?,?,?,?,?,'pending',?)",(str(uuid4()),row["turn_id"],count+index,canonical(proposal),canonical(native),timestamp(now)))
        messages.append({"role":"assistant","content":reply.content or None,"tool_calls":[native for _,native in checked]})
        await conn.execute("UPDATE chat_turns SET work_status='waiting',work_messages_json=? WHERE turn_id=?",(canonical(messages),row["turn_id"]))

    async def answer(self,principal,turn_id,value):
        async with self.chat.harness.operation, self.database.transaction() as conn:
            await self.chat.owner(conn,principal)
            encoded=canonical(value.model_dump(mode="json"))
            old=await one(conn,"SELECT * FROM chat_work_answers WHERE request_id=?",(str(value.request_id),))
            if old:
                if old["request_json"] != encoded or not await one(conn,"SELECT 1 FROM chat_work_steps s JOIN chat_turns t USING(turn_id) WHERE step_id=? AND turn_id=? AND owner_id=?",(old["step_id"],str(turn_id),str(principal.identity_id))):
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return
            turn=await one(conn,"SELECT * FROM chat_turns WHERE turn_id=? AND owner_id=?",(str(turn_id),str(principal.identity_id)))
            if not turn or not turn["work_grant_id"]:
                raise LookupError("work turn not found")
            step,grant=await permissions.allowed_step(conn,value.step_id,utc_now(),scope=self.chat.harness.scope(UUID(json.loads(turn["request_json"])["node_id"])))
            if not turn or step["turn_id"] != str(turn_id) or turn["revision"] != value.expected_revision or step["status"] != "permission":
                raise Conflict("IDEMPOTENCY_CONFLICT")
            text=value.text.strip().rstrip("。.!！")
            positive={"同意","允许","可以","确认","确认执行","同意写入","执行吧","好的","好","继续","批准","yes","ok","approve"}
            negative={"拒绝","不同意","不允许","不执行","取消","停止","不要","no","reject"}
            if text.lower() not in positive | negative:
                raise ValueError("请明确回复“同意”或“不同意”；修改内容请先停止本轮，再提出新请求。")
            decision="approve" if text.lower() in positive else "reject"
            await conn.execute("INSERT INTO chat_work_answers VALUES(?,?,?)",(str(value.request_id),str(value.step_id),encoded))
            await conn.execute("UPDATE chat_work_steps SET permission_decision=?,permission_text=?,status='pending' WHERE step_id=?",(decision,value.text,str(value.step_id)))
            await conn.execute("UPDATE chat_turns SET work_status='waiting',revision=revision+1 WHERE turn_id=?",(str(turn_id),))
            await audit(conn,str(principal.identity_id),None,"chat.work_"+decision,utc_now())

    async def abort(self,conn,turn_id,error,now):
        self.web.cancel(turn_id)
        await cancel_children(conn,turn_id,now)
        await conn.execute("UPDATE chat_work_steps SET status='stopped' WHERE turn_id=? AND status IN ('pending','waiting','permission')",(turn_id,))
        await conn.execute("UPDATE chat_turns SET work_status='',status=CASE WHEN status IN ('generating','stopping') THEN 'stopping' ELSE 'stopped' END,error_code=?,revision=revision+1 WHERE turn_id=?",(error,turn_id))
        await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id=(SELECT conversation_id FROM chat_turns WHERE turn_id=?)",(turn_id,))

    async def result(self,conn,step,data,now):
        row=await one(conn,"SELECT * FROM chat_turns WHERE turn_id=?",(step["turn_id"],))
        await permissions.allowed_step(conn,step["step_id"],now)
        data_json=canonical(data)
        if len(data_json.encode()) > 16384:
            data_json=canonical({"status":"unavailable","error_code":"WORK_RESULT_LIMIT","notice":"结果过大；请查看执行记录。"})
        messages=json.loads(row["work_messages_json"])
        messages.append({"role":"tool","tool_call_id":json.loads(step["native_json"])["id"],"content":data_json})
        from nexus.core.cognitive.work_catalog import functions
        # Leave room for the finite schema catalog and provider system prompt.
        if len(canonical(messages).encode())+len(canonical(functions(json.loads(row["work_catalog_json"]))).encode()) > 28000:
            raise AccessDenied("MODEL_INPUT_LIMIT")
        await conn.execute("UPDATE chat_work_steps SET status=?,result_json=? WHERE step_id=?",("succeeded" if data.get("status") == "succeeded" else "failed",data_json,step["step_id"]))
        pending=await one(conn,"SELECT 1 FROM chat_work_steps WHERE turn_id=? AND status IN ('pending','waiting','permission') LIMIT 1",(row["turn_id"],))
        await conn.execute("UPDATE chat_turns SET status=?,work_status=?,work_round=work_round+?,work_messages_json=?,response_text='',revision=revision+1 WHERE turn_id=?",("completed" if pending else "queued","waiting" if pending else "",int(not pending),canonical(messages),row["turn_id"]))
        await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id=?",(row["conversation_id"],))

    async def advance(self):
        await self.web.reap()
        async with self.database.transaction(write=False) as conn:
            steps=await conn.execute_fetchall("SELECT s.*,t.work_grant_id,t.owner_id,t.work_status,t.status AS turn_status,c.project_id,c.archived FROM chat_work_steps s JOIN chat_turns t USING(turn_id) JOIN conversations c USING(conversation_id) WHERE s.status IN ('pending','waiting','permission') ORDER BY s.rowid LIMIT 100")
        for step in steps:
            # Independent native calls still use the ordered device executor.
            if any(other["turn_id"]==step["turn_id"] and other["step_index"]<step["step_index"] for other in steps):
                continue
            try:
                await self.advance_step(step)
            except (AccessDenied,Conflict,ValueError,LookupError,PermissionError) as exc:
                async with self.chat.harness.operation, self.database.transaction() as conn:
                    await self.abort(conn,step["turn_id"],exc.code if isinstance(exc,AccessDenied) else "WORK_ACTION_UNAVAILABLE",utc_now())

    async def advance_step(self,step):
        now=utc_now()
        async with self.database.transaction() as conn:
            fresh,grant=await permissions.allowed_step(conn,step["step_id"],now,scope=self.chat.harness.scope(UUID((await one(conn,"SELECT node_id FROM chat_work_grants WHERE grant_id=?",(step["work_grant_id"],)))[0])))
            if (await one(conn,"SELECT paused FROM system_state WHERE singleton=1"))[0]:
                return
            if fresh["permission_decision"] == "reject":
                await self.abort(conn,step["turn_id"],"WORK_USER_DECLINED",now)
                return
            if fresh["status"] == "permission":
                return
            identity=await one(conn,"SELECT token_hash FROM identities WHERE identity_id=?",(grant["owner_id"],))
            principal=Principal(UUID(grant["owner_id"]),"owner",identity[0])
        proposal=json.loads(step["proposal_json"])
        node_id=UUID(grant["node_id"])
        intent=proposal["intent"]
        if intent in {"web_search","web_fetch"}:
            await self.web.start(step)
            return
        if intent == "get_node_status":
            data=await self.chat.harness.link.node_status(node_id)
            async with self.database.transaction() as conn:
                await permissions.operation(conn,step["step_id"],"status:"+step["step_id"],now)
                await self.result(conn,step,{"status":"succeeded","verification":"core_connection_observation","data":data},now)
            return
        if intent in {"run_tool","run_skill"}:
            await self.extension(step,principal,node_id,proposal)
            return
        preview=None
        if step["preview_task_id"]:
            async with self.database.transaction(write=False) as conn:
                task=await load_task(conn,UUID(step["preview_task_id"]))
            if task.status not in TERMINAL:
                return
            if task.status != "succeeded" or task.execution_outcome != "known" or not task.verification:
                raise AccessDenied("WRITE_PREVIEW_REQUIRED")
            preview=task.result.model_dump(mode="json")
        if not step["task_id"]:
            if intent == "write_file" and preview is not None and not fresh["permission_decision"] and not grant["auto_files"]:
                text=f"我需要写入 workspace/{proposal['arguments']['path']}。请查看下面的实际差异；回复“同意”将只批准本次内容，回复“不同意”则停止。"
                async with self.database.transaction() as conn:
                    await permissions.allowed_step(conn,step["step_id"],utc_now())
                    await conn.execute("UPDATE chat_work_steps SET status='permission',request_text=?,result_json=? WHERE step_id=?",(text,canonical({"preview":preview}),step["step_id"]))
                    await conn.execute("UPDATE chat_turns SET work_status='permission',revision=revision+1 WHERE turn_id=?",(step["turn_id"],))
                return
            if intent == "run_command" and grant["profile"] != "standard" and not fresh["permission_decision"]:
                async with self.database.transaction() as conn:
                    await permissions.allowed_step(conn,step["step_id"],utc_now())
                    await conn.execute("UPDATE chat_work_steps SET status='permission',request_text=? WHERE step_id=?",(f"需要执行已配置的固定命令 {proposal['arguments']['command_id']}。回复“同意”执行，回复“不同意”停止。",step["step_id"]))
                    await conn.execute("UPDATE chat_turns SET work_status='permission',revision=revision+1 WHERE turn_id=?",(step["turn_id"],))
                return
            role="preview" if intent == "write_file" and not preview else "execute"
            request=request_for(step,role,step["project_id"],self.chat.harness.scope(node_id).command_digests,preview)
            async with self.chat.harness.operation:
                await self.chat.harness.submit(principal,node_id,request,work_step_id=UUID(step["step_id"]),work_role=role,rate_limit=False)
            return
        async with self.database.transaction() as conn:
            task=await load_task(conn,UUID(step["task_id"]))
            if task.status not in TERMINAL:
                return
            data={"status":task.status,"execution_outcome":task.execution_outcome,"verification":task.verification}
            if task.status == "succeeded" and task.execution_outcome == "known" and task.verification:
                data["data"]=task.result.model_dump(mode="json")
            else:
                data["error_code"] = task.error.code if task.error else "EXECUTION_UNKNOWN" if task.execution_outcome == "unknown" else "WORK_ACTION_FAILED"
            if task.execution_outcome == "unknown":
                await conn.execute("UPDATE chat_work_steps SET result_json=? WHERE step_id=?",(canonical(data),step["step_id"]))
                await self.abort(conn,step["turn_id"],"EXECUTION_UNKNOWN",utc_now())
                return
            await self.result(conn,step,data,utc_now())

    async def extension(self,step,principal,node_id,proposal):
        arguments=proposal["arguments"]
        async with self.database.transaction(write=False) as conn:
            row=await one(conn,"SELECT * FROM chat_turns WHERE turn_id=?",(step["turn_id"],))
            snapshot=next((item for item in json.loads(row["work_catalog_json"]) if item.get("version_id") == arguments["version_id"] and item["name"] == proposal["intent"]),None)
            version=await one(conn,"SELECT * FROM extension_versions WHERE version_id=? AND owner_id=?",(arguments["version_id"],str(principal.identity_id)))
            if not snapshot or not version or version["artifact_digest"] != snapshot["artifact_digest"]:
                raise AccessDenied("VERSION_NOT_APPROVED")
            await self.chat.skills.extensions.current_approval(conn,version,utc_now())
        if proposal["intent"] == "run_skill":
            if not step["skill_run_id"]:
                await self.chat.skills.start(principal,UUID(arguments["version_id"]),node_id,arguments["input"],request_id=UUID(step["step_id"]),work_step_id=UUID(step["step_id"]))
                return
            run=await self.chat.skills.get(principal,UUID(step["skill_run_id"]))
            if run["status"] not in TERMINAL:
                return
            data={"status":run["status"],"verification":run["verification"],"data":run["result"] if run["status"] == "succeeded" else None,"error":run["error"]}
        else:
            await self.chat.skills.tools.execute_skill_step(owner_id=str(principal.identity_id),version=version,input_value=arguments["input"],work_step_id=step["step_id"])
            async with self.database.transaction(write=False) as conn:
                run=await one(conn,"SELECT * FROM extension_tool_runs WHERE work_step_id=?",(step["step_id"],))
            if not run:
                raise AccessDenied("SANDBOX_UNAVAILABLE")
            if run["status"] == "unknown":
                raise AccessDenied("EXECUTION_UNKNOWN")
            if run["status"] == "dispatching":
                return
            data={"status":run["status"],"verification":"trusted_controller_checked" if run["status"] == "succeeded" else None,"data":json.loads(run["output_json"]) if run["output_json"] else None}
        async with self.database.transaction() as conn:
            await self.result(conn,step,data,utc_now())
