"""Scoped native work: real authority/evidence, natural decisions and bounded cost."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest

from nexus.contracts.chat import ChatSubmission, ConversationCreate
from nexus.contracts.work import WorkGrantCreate, WorkPermissionReply
from nexus.core.cognitive import work_catalog
from nexus.core.config import TrustedNode
from nexus.core.security.types import AccessDenied
from nexus.core.storage.execution import ExecutionRepository, RESULT
from nexus.core.storage.database import one, utc_now
from nexus.node.tools.files import read_file, preview_write, write_file
from test_chat import rows
from test_chat_skills import app_for, prepare
from test_extensions_m4 import FakeSandbox, generate, run_approved, finish_task


class WorkProvider:
    def __init__(self, actions):
        self.actions=list(actions)
        self.calls=[]

    async def invoke(self,messages,receive,**options):
        from nexus.core.models.providers import ModelReply
        self.calls.append((messages,options))
        if not self.actions:
            return ModelReply("已根据实际工具结果完成。",100,20,True)
        proposal=self.actions.pop(0)
        native={"id":"work-call-"+str(len(self.calls)),"type":"function","function":{"name":proposal["intent"],"arguments":json.dumps(proposal["arguments"])}}
        return ModelReply("",100,20,True,finish_reason="tool_calls",proposal=proposal,native_tool_call=native)


async def setup(app,world,actions,*,profile="standard",max_operations=20):
    chat=app.state.chat
    chat.provider=WorkProvider(actions)
    conv=await chat.create(world.owner,ConversationCreate(request_id=uuid4()))
    grant=await chat.work.grant(world.owner,conv["conversation_id"],WorkGrantCreate(request_id=uuid4(),node_id=world.node_id,consent=True,profile=profile,max_operations=max_operations))
    async with world.db.transaction(write=False) as conn:
        directory=await work_catalog.catalog(chat,conn,world.owner,world.node_id)
    turn=await chat.submit(world.owner,conv["conversation_id"],ChatSubmission(request_id=uuid4(),expected_revision=conv["revision"],text="完成这项工作",allow_model=True,allow_tools=True,node_id=world.node_id,work_grant_id=UUID(grant["grant_id"]),work_digest=directory["work_digest"],allow_work_results=True))
    return conv,grant,turn


async def basic(app,world):
    tools={"system.info@1","filesystem.list@1","filesystem.read@1","filesystem.write_preview@1","filesystem.write@1"}
    app.state.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset(tools),roots=frozenset({"workspace"}))
    for key in tools:
        for identity in (world.owner_id,world.node_id):
            await world.security.set_grant(identity,key[:-2],root_id="" if key.startswith("system") else "workspace")


async def file_task(app,world,task_id,root):
    task=await world.tasks.get(UUID(task_id),world.owner_id)
    assert task.status == "ready"  # scoped approval; no second UI decision
    scope=app.state.harness.scope(world.node_id)
    repo=ExecutionRepository(world.db,world.security,32768)
    command=await repo.dispatch(task.task_id,world.node,scope,uuid4(),uuid4(),10)
    tool={"filesystem.read":read_file,"filesystem.write_preview":preview_write,"filesystem.write":write_file}[command.tool]
    output=tool(command.arguments,root)
    result=RESULT.validate_json(json.dumps({**command.model_dump(mode="json",include={"session_id","task_id","attempt_id","tool","tool_version","command_digest"}),"observed_at":utc_now().isoformat(),"outcome":"succeeded","result":output.model_dump(mode="json")}))
    await repo.store_result(world.node,result)
    await repo.verify(command.attempt_id)


def test_node_status_is_native_automatic_observation_and_one_budget(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat
        _,grant,turn=await setup(app,world,[{"intent":"get_node_status","arguments":{}}])
        await chat.process_one()
        assert (await chat.history(world.owner,turn["conversation_id"]))["active_turn_id"] == turn["turn_id"]
        await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"] == "completed" and done["model_call_count"] == 2
        assert done["work_steps"][0]["result"]["data"]["online"] is False
        assert done["work_steps"][0]["result"]["verification"] == "core_connection_observation"
        assert not await rows(world,"tasks")
        calls=await rows(world)
        assert all(c["work_root_turn_id"] == turn["turn_id"] for c in calls)
        assert sum(c["cost_microunits"] for c in calls) < 50000
        assert chat.provider.calls[1][0][-1]["tool_call_id"] == "work-call-1"
        assert (await chat.history(world.owner,turn["conversation_id"]))["work_grant"]["used_operations"] == 1
    asyncio.run(run())


def test_read_then_write_uses_real_preview_and_natural_bound_decision(world,monkeypatch,tmp_path):
    app=app_for(world,monkeypatch)
    root=tmp_path/"workspace";root.mkdir();(root/"note.txt").write_text("before",encoding="utf-8")
    async def run():
        await basic(app,world)
        chat=app.state.chat
        _,_,turn=await setup(app,world,[{"intent":"read_file","arguments":{"root_id":"workspace","path":"note.txt"}},
            {"intent":"write_file","arguments":{"root_id":"workspace","path":"note.txt","content":"after"}}])
        await chat.process_one();await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        await file_task(app,world,current["work_steps"][0]["task_id"],root)
        await chat.process_one();await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        await file_task(app,world,current["work_steps"][1]["preview_task_id"],root)
        await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        assert current["work_status"] == "permission" and (root/"note.txt").read_text() == "before"
        step=current["work_steps"][1]
        assert "+after" in step["result"]["preview"]["diff"]
        assert len(chat.provider.calls) == 2
        request=WorkPermissionReply(request_id=uuid4(),step_id=UUID(step["step_id"]),expected_revision=current["revision"],text="可以")
        await chat.work.answer(world.owner,UUID(turn["turn_id"]),request)
        await chat.work.answer(world.owner,UUID(turn["turn_id"]),request)
        await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        await file_task(app,world,current["work_steps"][1]["task_id"],root)
        await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"] == "completed" and (root/"note.txt").read_text() == "after"
        assert len(await rows(world,"tasks")) == 3 and len(await rows(world)) == 3
    asyncio.run(run())


@pytest.mark.parametrize("mode",["revoke","policy","restart","cancel_all","archive"])
def test_permission_changes_never_dispatch_pending_work(world,monkeypatch,mode):
    from nexus.contracts.chat import ConversationUpdate
    app=app_for(world,monkeypatch)
    async def run():
        await basic(app,world)
        chat=app.state.chat
        conv,_,turn=await setup(app,world,[{"intent":"read_file","arguments":{"root_id":"workspace","path":"note.txt"}}])
        await chat.process_one()
        if mode == "revoke":await chat.work.revoke(world.owner,conv["conversation_id"])
        elif mode == "policy":await world.security.set_grant(world.owner_id,"filesystem.list",root_id="workspace",enabled=False)
        elif mode == "restart":await chat.recover()
        elif mode == "archive":await chat.update(world.owner,conv["conversation_id"],ConversationUpdate(archived=True))
        else:await app.state.harness.control(world.owner,"cancel_all",uuid4())
        await chat.process_one()
        assert not await rows(world,"tasks") and len(await rows(world)) == 1
        current=await chat.get(world.owner,turn["turn_id"])
        assert current["recovery_required"] == (mode=="restart")
        assert current["work_status"] == ("waiting" if mode=="restart" else "")
    asyncio.run(run())


def test_enabled_skill_is_selected_and_directory_is_automatically_authorized(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        version=await prepare(app,world)
        chat=app.state.chat
        _,_,turn=await setup(app,world,[{"intent":"run_skill","arguments":{"version_id":version["version_id"],"input":{}}}])
        await chat.process_one();await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        run_id=current["work_steps"][0]["skill_run_id"]
        for _ in range(8):
            await app.state.skills.tick()
            run=await app.state.skills.get(world.owner,UUID(run_id))
            for step in run["steps"]:
                if step["task_id"] and step["status"] != "succeeded":
                    task=await world.tasks.get(UUID(step["task_id"]),world.owner_id)
                    if task.status != "succeeded":
                        assert task.status == "ready"
                        await finish_task(world,task.task_id)
            if run["status"] == "succeeded":break
        await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["work_steps"][0]["result"]["verification"] == "skill_steps_verified"
        assert done["model_call_count"] == 2
    asyncio.run(run())


def test_enabled_json_tool_uses_same_sandbox_journal_without_fake_skill(world,monkeypatch):
    app=app_for(world,monkeypatch,sandbox=True)
    fake=FakeSandbox();app.state.extensions.sandbox.transport=fake
    async def run():
        version=await run_approved(app,world,await generate(app,world,"tool","统计目录",node_id=world.node_id))
        await app.state.extensions.enable(world.owner,UUID(version["version_id"]))
        fake.calls.clear()
        chat=app.state.chat
        _,_,turn=await setup(app,world,[{"intent":"run_tool","arguments":{"version_id":version["version_id"],"input":{"entries":[{"name":"a","kind":"file"}]}}}])
        await chat.process_one();await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["work_steps"][0]["result"]["data"]["total"] == 1
        assert len(fake.calls) == 1 and not await rows(world,"skill_runs")
        assert len(await rows(world,"extension_tool_runs")) == 1
    asyncio.run(run())


def test_model_round_and_operation_limits_cannot_be_widened(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat
        _,_,turn=await setup(app,world,[{"intent":"get_node_status","arguments":{}}]*5)
        for _ in range(7):await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["model_call_count"] == 5 and len(done["work_steps"]) == 4
        assert done["status"] == "failed" and done["error_code"] == "POLICY_DENIED"
        assert chat.provider.calls[-1][1]["allow_tools"] is False
    asyncio.run(run())


@pytest.mark.parametrize("profile",["read","standard"])
def test_fixed_command_profile_and_natural_permission(world,monkeypatch,tmp_path,profile):
    from test_basic_tools import template
    from nexus.core.config import TrustedNode
    app=app_for(world,monkeypatch)
    command=template(["--version"])
    async def run():
        app.state.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset({"command.run@1"}),roots=frozenset({"workspace"}),command_digests={"version":command.digest()})
        for identity in (world.owner_id,world.node_id):
            await world.security.set_grant(identity,"command.run",root_id="workspace")
        chat=app.state.chat
        _,_,turn=await setup(app,world,[{"intent":"run_command","arguments":{"root_id":"workspace","command_id":"version"}}],profile=profile)
        await chat.process_one();await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        if profile == "read":
            assert current["work_status"] == "permission" and not await rows(world,"tasks")
            await chat.work.answer(world.owner,UUID(turn["turn_id"]),WorkPermissionReply(request_id=uuid4(),step_id=UUID(current["work_steps"][0]["step_id"]),expected_revision=current["revision"],text="同意"))
            await chat.process_one()
        task=(await rows(world,"tasks"))[0]
        assert task["status"] == "ready" and json.loads(task["action_json"])["arguments"]["template_digest"] == command.digest()
        await chat.work.revoke(world.owner,turn["conversation_id"])
        assert (await world.tasks.get(UUID(task["task_id"]),world.owner_id)).status == "cancelled"
    asyncio.run(run())


def test_one_operation_grant_still_allows_final_answer_but_no_more_effects(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat
        _,_,turn=await setup(app,world,[{"intent":"get_node_status","arguments":{}}],max_operations=1)
        await chat.process_one();await chat.process_one()
        assert (await chat.get(world.owner,turn["turn_id"]))["status"] == "completed"
    asyncio.run(run())
