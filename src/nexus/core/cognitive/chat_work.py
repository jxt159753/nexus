"""Bounded chat work coordination; all effects use the existing executors."""

import json
from nexus.core.cognitive import node_routing
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from nexus.contracts.extensions import canonical
from nexus.core.cognitive import work_catalog
from nexus.core.cognitive.chat_context import fit_work_context
from nexus.core.cognitive import chat_compaction
from nexus.core.cognitive.chat_skills import eligible as eligible_skill
from nexus.core.security import work as permissions
from nexus.core.security.service import scope_digest
from nexus.core.security.types import AccessDenied, Principal
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import REQUEST, Conflict, TERMINAL, TaskRecord, load_task
from nexus.core.storage.control import cancel_task


WORK_INTENTS = {"read":"read_file_lines","glob":"glob_files","grep":"search_file_content","edit":"edit_file"}


def task_feedback(task: TaskRecord) -> dict:
    """The same bounded failure evidence for execution and write previews."""
    data = {"status": task.status, "execution_outcome": task.execution_outcome, "verification": task.verification}
    if task.status == "succeeded" and task.execution_outcome == "known" and task.verification:
        data["data"] = task.result.model_dump(mode="json")
        if task.action.tool == "command.run" and task.result.exit_code != 0:
            data.update(status="failed", error_code="COMMAND_EXIT_NONZERO")
    else:
        data["error_code"] = task.error.code.value if task.error else "EXECUTION_UNKNOWN" if task.execution_outcome == "unknown" else "WORK_ACTION_FAILED"
        if task.error:
            data["error"] = task.error.model_dump(mode="json")
    return data


def request_for(step, role, project_id, command_digests, preview=None, interaction_digests=None, file_source=None):
    proposal = json.loads(step["proposal_json"])
    intent, arguments = WORK_INTENTS.get(proposal["intent"],proposal["intent"]), dict(proposal["arguments"])
    request_id=uuid5(NAMESPACE_URL,f"nexus-work:{step['step_id']}:{role}")
    if intent == 'send_file':
        from nexus.contracts.file_transfer import receiving_path
        if file_source is None:
            raise AccessDenied('FILE_SOURCE_UNAVAILABLE')
        intent='receive_file'
        arguments={key:file_source[key] for key in ('attachment_id','conversation_id','sha256','size_bytes')}
        arguments.update(root_id='workspace',path=receiving_path(request_id,file_source['filename']))
    if intent in {"observe_target","interact_target"}:
        arguments["template_digest"]=(interaction_digests or {}).get(arguments["target_id"],"")
    if intent == "run_command":
        arguments["template_digest"] = command_digests.get(arguments["command_id"], "")
    if intent in {"write_file","edit_file"}:
        if role == "preview":
            intent = "preview_file_write" if intent == "write_file" else "preview_file_edit"
        else:
            if not preview:
                raise AccessDenied("WRITE_PREVIEW_REQUIRED")
            arguments.update(expected_sha256=preview["previous_sha256"], preview_task_id=step["preview_task_id"],preview_digest=preview["preview_digest"])
            if intent == "edit_file":
                arguments.update(content_sha256=preview["content_sha256"],bytes_written=preview["bytes_written"])
    return REQUEST.validate_json(canonical({"request_id":str(request_id),"project_id":project_id,"intent":intent,"arguments":arguments}))


async def validate_task(conn, step_id, role, owner_id, node_id, request, now):
    step, grant = await permissions.allowed_step(conn,step_id,now)
    if role not in {"preview","execute"} or grant["owner_id"] != str(owner_id) or grant["node_id"] != str(node_id):
        raise AccessDenied("POLICY_DENIED")
    project = await one(conn,"SELECT c.project_id FROM conversations c JOIN chat_turns t USING(conversation_id) WHERE t.turn_id=?",(step["turn_id"],))
    proposal = json.loads(step["proposal_json"])
    arguments = proposal["arguments"]
    if proposal['intent']=='send_file':
        from nexus.core.cognitive.chat_files import bound_attachment
        source=await bound_attachment(conn,owner_id,grant['conversation_id'],arguments)
        expected=request_for(step,role,project[0],{},file_source=source)
        if role!='execute' or request!=expected:
            raise AccessDenied('POLICY_DENIED')
        return
    if request.intent == 'execute_plugin' and proposal['intent'] == 'plugin_execute' and role == 'execute':
        if (request.arguments.plugin_id != arguments['plugin_id'] or request.arguments.tool != arguments['tool']
                or request.arguments.input != arguments['input'] or request.arguments.access_root != arguments.get('access_root')
                or str(request.arguments.invocation_id) != str(step_id)
                or str(request.arguments.context_id) != (await one(conn,'SELECT conversation_id FROM chat_turns WHERE turn_id=?',(step['turn_id'],)))[0]):
            raise AccessDenied('POLICY_DENIED')
        return
    if (request.intent,proposal["intent"]) in {("preview_file_write","write_file"),("preview_file_edit","edit")} and role == "preview":
        pass
    elif request.intent != WORK_INTENTS.get(proposal["intent"],proposal["intent"]) or role != "execute":
        raise AccessDenied("POLICY_DENIED")
    actual = request.arguments.model_dump(mode="json")
    if any(actual.get(key) != value for key,value in arguments.items()) or project[0] != (str(request.project_id) if request.project_id else None):
        raise AccessDenied("POLICY_DENIED")
    if request.intent in {"write_file","edit_file"} and (not grant["auto_files"] and step["permission_decision"] != "approve" and grant["permission_mode"] != "full" or str(request.arguments.preview_task_id) != step["preview_task_id"]):
        raise AccessDenied("WORK_CONFIRMATION_REQUIRED")
    if request.intent == "run_command" and grant["profile"] != "standard" and step["permission_decision"] != "approve" and grant["permission_mode"] != "full":
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
        self.feedback_pending = set()

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
            await conn.execute("UPDATE chat_work_grants SET operation_limits_enabled=? WHERE grant_id=?", (int(self.chat.config.call_limits_enabled), identifier))
            await audit(conn,str(principal.identity_id),None,"chat.work_granted",now)
            from nexus.core.security.modes import bind_grant
            await bind_grant(conn, identifier, conversation_id)
            return self.public_grant(await one(conn,"SELECT * FROM chat_work_grants WHERE grant_id=?",(identifier,)))

    @staticmethod
    def public_grant(row):
        return {key:row[key] for key in ("grant_id","conversation_id","node_id","profile","max_operations","used_operations","operation_limits_enabled","expires_at","revoked_at","permission_mode")}

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
        rows = await conn.execute_fetchall("SELECT s.*,g.node_id FROM chat_work_steps s JOIN chat_turns t USING(turn_id) LEFT JOIN chat_work_grants g ON g.grant_id=COALESCE(s.routed_grant_id,t.work_grant_id) WHERE s.turn_id=? ORDER BY step_index",(str(turn_id),))
        labels = await node_routing.names(conn,self.chat.harness.link.config)
        output=[]
        for row in rows:
            value=dict(row)
            value['node_label'] = labels.get(value['node_id'],'设备')
            value["proposal"] = json.loads(value.pop("proposal_json"))
            value["result"] = json.loads(value.pop("result_json")) if row["result_json"] else None
            value.pop("native_json")
            output.append(value)
        return output

    async def prepare(self,conn,principal,conversation_id,value):
        if value.all_nodes:
            from nexus.core.cognitive.node_routing import prepare
            return await prepare(self,conn,principal,conversation_id,value)
        directory = await work_catalog.catalog(self.chat,conn,principal,value.node_id)
        if directory["work_digest"] != value.work_digest:
            raise Conflict("IDEMPOTENCY_CONFLICT")
        if value.workspace_work:
            # Existing configured workspace + persisted identity grants are the
            # authorization. This internal scope is not an extra user approval.
            if not {"get_node_status","get_system_info","android_device_status","android_notification_digest","read_file","find_files","write_file","read","glob","grep","edit","web_search","web_fetch","read_image","plugin_inspect","plugin_execute","skill_create"}.intersection(item["name"] for item in directory["catalog"]):
                raise AccessDenied("POLICY_DENIED")
            now=utc_now()
            if (await one(conn,"SELECT count(*) FROM chat_work_grants"))[0]>=10000:
                raise Conflict("BUSY")
            identifier=str(uuid4())
            await conn.execute("INSERT INTO chat_work_grants(grant_id,conversation_id,owner_id,node_id,request_id,request_json,profile,scope_digest,policy_version,max_operations,created_at,expires_at,auto_files) VALUES(?,?,?,?,?,?,'standard',?,?,20,?,?,1)",(
                identifier,str(conversation_id),str(principal.identity_id),str(value.node_id),str(value.request_id),canonical({"source":"configured_workspace","request_id":str(value.request_id)}),scope_digest(self.chat.harness.scope(value.node_id)),(await one(conn,"SELECT version FROM policy_state WHERE singleton=1"))[0],timestamp(now),timestamp(now+timedelta(minutes=5))))
            await conn.execute("UPDATE chat_work_grants SET operation_limits_enabled=? WHERE grant_id=?", (int(self.chat.config.call_limits_enabled), identifier))
            await audit(conn,str(principal.identity_id),None,"chat.workspace_scope",now)
            from nexus.core.security.modes import bind_grant
            await bind_grant(conn, identifier, conversation_id)
            return directory["catalog"],identifier
        grant = await permissions.valid_grant(conn,value.work_grant_id,utc_now(),owner_id=principal.identity_id,node_id=value.node_id,scope=self.chat.harness.scope(value.node_id))
        if grant["conversation_id"] != str(conversation_id) or grant["operation_limits_enabled"] and grant["used_operations"] >= grant["max_operations"]:
            raise AccessDenied("WORK_PERMISSION_EXPIRED")
        return directory["catalog"],str(value.work_grant_id)

    async def model_start(self,conn,row,now):
        await node_routing.validate_turn(self.chat,conn,row,now)
        if self.chat.config.call_limits_enabled and row["work_round"] > self.chat.config.chat_work_max_actions:
            raise AccessDenied("WORK_OPERATION_LIMIT")
        calls = await chat_compaction.root_calls(conn,row["turn_id"])
        if sum(call["web_step_id"] is None and not call["compaction_call"] for call in calls) != row["work_round"]:
            raise AccessDenied("BUDGET_EXCEEDED")
        remaining = chat_compaction.remaining(self.chat.config, calls)
        messages = json.loads(row["work_messages_json"] or row["context_surface_json"] or row["context_json"])
        if json.loads(row["request_json"]).get("allow_compaction"):
            return remaining, messages
        count = (await one(conn,"SELECT count(*) FROM chat_work_steps WHERE turn_id=?",(row["turn_id"],)))[0]
        options = self.chat.model_options(row, self.chat.config.work_actions_remaining(count))
        projected = fit_work_context(self.chat.config, messages, options["work_catalog"],
                                     allow_tools=options["allow_tools"], work_limit=options["work_limit"],
                                     generate_title=options["generate_title"],history_summary=json.loads(row["history_summary_json"]),auto_memory=options.get('auto_memory',False))
        if projected != messages:
            await conn.execute("UPDATE chat_turns SET work_messages_json=?,context_truncated=1 WHERE turn_id=?",(canonical(projected),row["turn_id"]))
            await audit(conn,row["owner_id"],None,"chat.context_compacted",now)
        return remaining, projected

    async def proposed(self,conn,row,reply,now):
        if row["phase"] == "summary":
            raise AccessDenied("POLICY_DENIED")
        count=(await one(conn,"SELECT count(*) FROM chat_work_steps WHERE turn_id=?",(row["turn_id"],)))[0]
        calls=reply.work_calls or ((reply.proposal,reply.native_tool_call),)
        if (self.chat.config.call_limits_enabled and row["work_round"] >= self.chat.config.chat_work_max_actions) or not 1<=len(calls)<=min(4,self.chat.config.work_actions_remaining(count)):
            raise AccessDenied("WORK_STEP_LIMIT")
        entries=json.loads(row["work_catalog_json"])
        checked=[(work_catalog.checked(entries,proposal,native),native) for proposal,native in calls]
        await node_routing.validate_turn(self.chat,conn,row,now)
        messages=json.loads(row["work_messages_json"] or row["context_json"])
        identifiers=[native["id"] for _,native in checked]
        old_ids={call["id"] for message in messages for call in message.get("tool_calls",[])}
        if len(set(identifiers))!=len(identifiers) or old_ids.intersection(identifiers):
            raise AccessDenied("MODEL_RESPONSE_INVALID")
        for index,(proposal,native) in enumerate(checked,1):
            proposal, routed = await node_routing.bind_step(conn,row,proposal,now)
            await conn.execute("INSERT INTO chat_work_steps(step_id,turn_id,step_index,proposal_json,native_json,status,created_at,routed_grant_id) VALUES(?,?,?,?,?,'pending',?,?)",(str(uuid4()),row["turn_id"],count+index,canonical(proposal),canonical(native),timestamp(now),routed))
        messages.append({"role":"assistant","content":reply.content or None,"tool_calls":[native for _,native in checked]})
        await conn.execute("UPDATE chat_turns SET work_status='waiting',work_messages_json=? WHERE turn_id=?",(canonical(messages),row["turn_id"]))

    async def rejected(self,conn,row,reply,now):
        """Known argument rejection has no execution; use the existing exchange budget."""
        count=(await one(conn,"SELECT count(*) FROM chat_work_steps WHERE turn_id=?",(row["turn_id"],)))[0]
        if (self.chat.config.call_limits_enabled and row["work_round"] >= self.chat.config.chat_work_max_actions) or not 1 <= len(reply.rejected_calls) <= min(4,self.chat.config.work_actions_remaining(count)) or not reply.validation_errors:
            raise AccessDenied("WORK_STEP_LIMIT")
        await node_routing.validate_turn(self.chat,conn,row,now)
        messages=json.loads(row["work_messages_json"] or row["context_json"])
        identifiers=[native["id"] for native in reply.rejected_calls]
        old_ids={call["id"] for message in messages for call in message.get("tool_calls",[])}
        if len(set(identifiers)) != len(identifiers) or old_ids.intersection(identifiers):
            raise AccessDenied("MODEL_RESPONSE_INVALID")
        messages.append({"role":"assistant","content":reply.content or None,"tool_calls":list(reply.rejected_calls)})
        for index,native in enumerate(reply.rejected_calls):
            evidence={"status":"failed","phase":"arguments","execution_outcome":"not_started","batch_executed":False,
                      "error_code":"INVALID_TOOL_ARGUMENTS","errors":[item for item in reply.validation_errors if item["index"]==index],
                      "message":"整批调用均未执行。按工具契约修正参数；write_file 每个文件正文最多 12000 UTF-8 字节，可精简或在剩余动作内拆文件。"}
            messages.append({"role":"tool","tool_call_id":native["id"],"content":canonical(evidence)})
        await conn.execute("UPDATE chat_turns SET work_round=work_round+1,work_messages_json=? WHERE turn_id=?",(canonical(messages),row["turn_id"]))
        await audit(conn,row["owner_id"],None,"chat.tool_arguments_rejected",now)

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
            step,grant=await permissions.allowed_step(conn,value.step_id,utc_now(),scope=self.chat.harness.scope(UUID((await permissions.step_grant(conn,value.step_id))["node_id"])))
            if not turn or step["turn_id"] != str(turn_id) or turn["revision"] != value.expected_revision or step["status"] != "permission":
                raise Conflict("IDEMPOTENCY_CONFLICT")
            text=value.text.strip().rstrip("。.!！")
            proposal = json.loads(step['proposal_json'])
            question = (proposal.get('intent')=='plugin_execute' and proposal['arguments']['plugin_id']=='builtin.interaction'
                        and proposal['arguments']['tool']=='ask_user_question')
            positive={"同意","允许","可以","确认","确认执行","同意写入","执行吧","好的","好","继续","批准","yes","ok","approve"}
            negative={"拒绝","不同意","不允许","不执行","取消","停止","不要","no","reject"}
            if not question and text.lower() not in positive | negative:
                raise ValueError("请明确回复“同意”或“不同意”；修改内容请先停止本轮，再提出新请求。")
            decision="approve" if question or text.lower() in positive else "reject"
            await conn.execute("INSERT INTO chat_work_answers VALUES(?,?,?)",(str(value.request_id),str(value.step_id),encoded))
            await conn.execute("UPDATE chat_work_steps SET permission_decision=?,permission_text=?,status='pending' WHERE step_id=?",(decision,value.text,str(value.step_id)))
            await conn.execute("UPDATE chat_turns SET work_status='waiting',revision=revision+1 WHERE turn_id=?",(str(turn_id),))
            await audit(conn,str(principal.identity_id),None,"chat.work_"+decision,utc_now())

    async def abort(self,conn,turn_id,error,now):
        self.feedback_pending.add(turn_id)
        self.web.cancel(turn_id)
        await cancel_children(conn,turn_id,now)
        await conn.execute("UPDATE chat_work_steps SET status='stopped' WHERE turn_id=? AND status IN ('pending','waiting','permission')",(turn_id,))
        await conn.execute("UPDATE chat_turns SET work_status='',status=CASE WHEN status IN ('generating','stopping') THEN 'stopping' ELSE 'stopped' END,error_code=?,revision=revision+1 WHERE turn_id=?",(error,turn_id))
        await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id=(SELECT conversation_id FROM chat_turns WHERE turn_id=?)",(turn_id,))

    async def result(self,conn,step,data,now):
        from nexus.core.cognitive.work_images import model_result, append_batch_images, image_proposal
        row=await one(conn,"SELECT * FROM chat_turns WHERE turn_id=?",(step["turn_id"],))
        await permissions.allowed_step(conn,step["step_id"],now)
        data_json=canonical(data)
        messages=json.loads(row["work_messages_json"])
        messages.append({"role":"tool","tool_call_id":json.loads(step["native_json"])["id"],"content":canonical(model_result(data,is_image=image_proposal(json.loads(step["proposal_json"]))))})
        await conn.execute("UPDATE chat_work_steps SET status=?,result_json=? WHERE step_id=?",("succeeded" if data.get("status") == "succeeded" else "failed",data_json,step["step_id"]))
        pending=await one(conn,"SELECT 1 FROM chat_work_steps WHERE turn_id=? AND status IN ('pending','waiting','permission') LIMIT 1",(row["turn_id"],))
        if not pending:
            await append_batch_images(conn, row["turn_id"], messages)
        count = (await one(conn,"SELECT count(*) FROM chat_work_steps WHERE turn_id=?",(row["turn_id"],)))[0]
        options = self.chat.model_options({**dict(row), "work_round": row["work_round"]+1}, self.chat.config.work_actions_remaining(count))
        try:
            projected = messages if json.loads(row["request_json"]).get("allow_compaction") else fit_work_context(self.chat.config, messages, options["work_catalog"],
                                         allow_tools=options["allow_tools"], work_limit=options["work_limit"],history_summary=json.loads(row["history_summary_json"]),auto_memory=options.get('auto_memory',False))
        except AccessDenied as exc:
            # A full model context cannot erase an already verified execution.
            await self.abort(conn,row["turn_id"],exc.code,now)
            return
        if projected != messages:
            await conn.execute("UPDATE chat_turns SET context_truncated=1 WHERE turn_id=?",(row["turn_id"],))
            await audit(conn,row["owner_id"],None,"chat.context_compacted",now)
        await conn.execute("UPDATE chat_turns SET status=?,work_status=?,work_round=work_round+?,work_messages_json=?,response_text='',revision=revision+1 WHERE turn_id=?",("completed" if pending else "queued","waiting" if pending else "",int(not pending),canonical(projected),row["turn_id"]))
        await conn.execute("UPDATE conversations SET revision=revision+1 WHERE conversation_id=?",(row["conversation_id"],))

    async def advance(self):
        await self.web.reap()
        async with self.database.transaction(write=False) as conn:
            steps=await conn.execute_fetchall("SELECT s.*,COALESCE(s.routed_grant_id,t.work_grant_id) AS work_grant_id,t.owner_id,t.work_status,t.status AS turn_status,c.project_id,c.archived FROM chat_work_steps s JOIN chat_turns t USING(turn_id) JOIN conversations c USING(conversation_id) WHERE t.recovery_required=0 AND s.status IN ('pending','waiting','permission') ORDER BY s.rowid LIMIT 100")
        for step in steps:
            # Independent native calls still use the ordered device executor.
            if any(other["turn_id"]==step["turn_id"] and other["step_index"]<step["step_index"] for other in steps):
                continue
            try:
                await self.advance_step(step)
            except (AccessDenied,Conflict,ValueError,LookupError,PermissionError) as exc:
                async with self.chat.harness.operation, self.database.transaction() as conn:
                    error = exc.code if isinstance(exc,AccessDenied) else "WORK_ACTION_UNAVAILABLE"
                    now = utc_now()
                    try:
                        fresh, _ = await permissions.allowed_step(conn, step["step_id"], now,
                            scope=self.chat.harness.scope(UUID((await one(conn,"SELECT node_id FROM chat_work_grants WHERE grant_id=?",(step["work_grant_id"],)))[0])))
                    except AccessDenied:
                        await self.abort(conn,step["turn_id"],error,now)
                        continue
                    # Only failures proven to precede all child execution may
                    # resume. Authorization, conflicts and unknown effects stop.
                    no_child = not any(fresh[key] for key in ("task_id", "preview_task_id", "skill_run_id", "tool_run_id"))
                    if no_child and (not isinstance(exc,(AccessDenied,Conflict)) and isinstance(exc,(ValueError,LookupError,PermissionError))
                                     or error in {"NODE_OFFLINE", "SANDBOX_UNAVAILABLE"}):
                        await self.result(conn,fresh,{"status":"failed","execution_outcome":"not_started",
                            "phase":"prepare","error_code":error},now)
                    else:
                        await self.abort(conn,step["turn_id"],error,now)

        from nexus.core.cognitive import chat_feedback
        for turn_id in tuple(self.feedback_pending):
            self.feedback_pending.discard(turn_id)
            async with self.chat.harness.operation, self.database.transaction() as conn:
                try:
                    if await chat_feedback.queue(self, conn, turn_id, utc_now()) is False:
                        self.feedback_pending.add(turn_id)
                except AccessDenied:
                    # No extra outbound consent or budget is inferred for feedback.
                    pass

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
        async with self.database.transaction() as conn:
            if not step["skill_run_id"] and not step["tool_run_id"] and await one(conn,"SELECT 1 FROM chat_controls WHERE turn_id=? AND kind='steer' AND applied_call_id IS NULL",(step["turn_id"],)):
                identifier=step["task_id"] or step["preview_task_id"]
                can_skip=True
                if identifier:
                    child=await load_task(conn,UUID(identifier))
                    if child.current_attempt_id is not None:
                        # Finish collecting a dispatched operation before the correction.
                        can_skip=child.status in TERMINAL and not step["task_id"]
                        if child.status not in TERMINAL:
                            return
                    elif child.status not in TERMINAL:
                        await cancel_task(conn,child,child.owner_id,utc_now())
                # Resolve every pending call with an explicit non-execution fact.
                # A preview has no mutation; its original result remains on its task.
                if can_skip:
                    await self.result(conn,step,{"status":"not_started","execution_outcome":"not_started","reason":"user_correction_before_dispatch"},utc_now())
                    return
        proposal=json.loads(step["proposal_json"])
        node_id=UUID(grant["node_id"])
        intent=proposal["intent"]
        if intent == "skill_create":
            from nexus.core.cognitive.work_creation import create_skill
            await create_skill(self.chat, step, principal, node_id)
            return
        if intent in {'inspect_ui_extension','submit_ui_extension'}:
            await self.chat.plugins.ui_extensions.work_step(step, principal, node_id, proposal)
            return
        if intent in {'plugin_inspect','plugin_execute','plugin_install'}:
            await self.chat.plugins.work_step(step,principal,node_id,proposal)
            return
        from nexus.core.cognitive import work_queries
        if intent in work_queries.NAMES:
            async with self.chat.harness.operation, self.database.transaction() as conn:
                fresh, current_grant = await permissions.allowed_step(conn,step["step_id"],utc_now(),scope=self.chat.harness.scope(node_id))
                await self.chat.owner(conn,principal)
                await permissions.operation(conn,step["step_id"],"query:"+step["step_id"],utc_now())
                data = await work_queries.query(conn,fresh,current_grant,intent,proposal["arguments"],utc_now())
                await self.result(conn,fresh,{"status":"succeeded","verification":"core_record_snapshot","data":data},utc_now())
            return
        if intent == "read_image" and not self.chat.config.chat_images_enabled:
            raise AccessDenied("POLICY_DENIED")
        if intent in {"web_search","web_fetch"}:
            await self.web.start(step)
            return
        if intent == "inspect_capability":
            async with self.chat.harness.operation, self.database.transaction() as conn:
                await permissions.allowed_step(conn,step["step_id"],utc_now(),scope=self.chat.harness.scope(node_id))
                row=await one(conn,"SELECT work_catalog_json FROM chat_turns WHERE turn_id=?",(step["turn_id"],))
                entry=work_catalog.capability_snapshot(json.loads(row[0]),node_id,proposal['arguments']['version_id'],intent)
                version=await one(conn,"SELECT * FROM extension_versions WHERE version_id=? AND owner_id=?",(proposal["arguments"]["version_id"],str(principal.identity_id)))
                if not entry or not version or entry["artifact_digest"] != version["artifact_digest"] or self.chat.skills is None:
                    raise AccessDenied("VERSION_NOT_APPROVED")
                self.chat.skills.extensions.require_enabled()
                await self.chat.skills.extensions.current_approval(conn,version,utc_now())
                await permissions.operation(conn,step["step_id"],"inspect:"+step["step_id"],utc_now())
                details = dict(entry)
                if version["kind"] == "skill":
                    await eligible_skill(self.chat.skills,conn,str(principal.identity_id),version,node_id)
                    details.update(markdown=version["markdown_text"],steps=json.loads(version["steps_json"]))
                await self.result(conn,step,{"status":"succeeded","verification":"approved_version_snapshot","data":details},utc_now())
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
                data = task_feedback(task)
                data.update(phase="preview", write_executed=False)
                if data["status"] == "succeeded":
                    data.update(status="failed", error_code="WRITE_PREVIEW_REQUIRED")
                async with self.database.transaction() as conn:
                    if task.execution_outcome == "unknown":
                        await conn.execute("UPDATE chat_work_steps SET result_json=? WHERE step_id=?",(canonical(data),step["step_id"]))
                        await self.abort(conn,step["turn_id"],"EXECUTION_UNKNOWN",utc_now())
                    else:
                        await self.result(conn,step,data,utc_now())
                return
            preview=task.result.model_dump(mode="json")
        if not step["task_id"]:
            if intent in {"write_file","edit"} and preview is not None and not fresh["permission_decision"] and not grant["auto_files"] and grant['permission_mode'] != 'full':
                text=f"我需要写入 workspace/{proposal['arguments']['path']}。请查看下面的实际差异；回复“同意”将只批准本次内容，回复“不同意”则停止。"
                async with self.database.transaction() as conn:
                    await permissions.allowed_step(conn,step["step_id"],utc_now())
                    await conn.execute("UPDATE chat_work_steps SET status='permission',request_text=?,result_json=? WHERE step_id=?",(text,canonical({"preview":preview}),step["step_id"]))
                    await conn.execute("UPDATE chat_turns SET work_status='permission',revision=revision+1 WHERE turn_id=?",(step["turn_id"],))
                return
            if intent == "interact_target" and not fresh["permission_decision"] and (grant["auto_files"] or grant["profile"]!="standard") and grant['permission_mode'] != 'full':
                async with self.database.transaction() as conn:
                    prior=await conn.execute_fetchall("SELECT proposal_json FROM chat_work_steps WHERE turn_id=? AND permission_decision='approve'",(step["turn_id"],))
                    if not any(json.loads(item[0])["intent"]=="interact_target" and json.loads(item[0])["arguments"]["target_id"]==proposal["arguments"]["target_id"] for item in prior):
                        await conn.execute("UPDATE chat_work_steps SET status='permission',request_text=? WHERE step_id=?",(f"本轮需要操作已配置目标 {proposal['arguments']['target_id']}，仅允许该目标的固定动作，不含其他程序或页面范围。回复“同意”授权本轮该目标，回复“不同意”停止。",step["step_id"]))
                        await conn.execute("UPDATE chat_turns SET work_status='permission',revision=revision+1 WHERE turn_id=?",(step["turn_id"],))
                        return
            if intent == "run_command" and grant["profile"] != "standard" and not fresh["permission_decision"] and grant["permission_mode"] != "full":
                async with self.database.transaction() as conn:
                    await permissions.allowed_step(conn,step["step_id"],utc_now())
                    await conn.execute("UPDATE chat_work_steps SET status='permission',request_text=? WHERE step_id=?",(f"需要执行已配置的固定命令 {proposal['arguments']['command_id']}。回复“同意”执行，回复“不同意”停止。",step["step_id"]))
                    await conn.execute("UPDATE chat_turns SET work_status='permission',revision=revision+1 WHERE turn_id=?",(step["turn_id"],))
                return
            role="preview" if intent in {"write_file","edit"} and not preview else "execute"
            file_source=None
            if intent=='send_file':
                from nexus.core.cognitive.chat_files import materialize
                file_source=await materialize(self.chat,principal,grant['conversation_id'],**{
                    'kind':proposal['arguments']['source_kind'],'identifier':proposal['arguments']['source_id']})
            request=request_for(step,role,step["project_id"],self.chat.harness.scope(node_id).command_digests,preview,self.chat.harness.scope(node_id).interaction_digests,file_source)
            async with self.chat.harness.operation:
                await self.chat.harness.submit(principal,node_id,request,work_step_id=UUID(step["step_id"]),work_role=role,rate_limit=False)
            return
        async with self.database.transaction() as conn:
            task=await load_task(conn,UUID(step["task_id"]))
            if task.status not in TERMINAL:
                return
            data = task_feedback(task)
            if task.execution_outcome == "unknown":
                await conn.execute("UPDATE chat_work_steps SET result_json=? WHERE step_id=?",(canonical(data),step["step_id"]))
                await self.abort(conn,step["turn_id"],"EXECUTION_UNKNOWN",utc_now())
                return
            await self.result(conn,step,data,utc_now())

    async def extension(self,step,principal,node_id,proposal):
        arguments=proposal["arguments"]
        async with self.database.transaction(write=False) as conn:
            row=await one(conn,"SELECT * FROM chat_turns WHERE turn_id=?",(step["turn_id"],))
            snapshot=work_catalog.capability_snapshot(json.loads(row['work_catalog_json']),node_id,arguments['version_id'],proposal['intent'])
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
            if run["error_json"]:
                data["error"] = json.loads(run["error_json"])
                data["error_code"] = data["error"]["code"]
        async with self.database.transaction() as conn:
            await self.result(conn,step,data,utc_now())
