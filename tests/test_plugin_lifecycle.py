"""Lifecycle protocol simulation plus real task/Node integration; no paid calls."""

import asyncio
import json
import os
from uuid import UUID,uuid4

import pytest
from nexus.contracts.chat import ChatSubmission
from nexus.core.config import TrustedNode
from nexus.core.plugins.lifecycle import tick,budget,recover
from nexus.core.security.modes import ModeChange,PermissionHTTP
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one
from nexus.plugins.contracts import PluginRelease
from test_chat_skills import app_for
from test_chat_work import setup


def candidate():
    return PluginRelease.model_validate({'manifest':{'plugin_id':'user.add_numbers','version':'1.0.0','name':'加法','description':'返回两个整数之和','license':'MIT',
        'tools':[{'name':'add','description':'加法','input_schema':{'type':'object','properties':{'a':{'type':'integer'},'b':{'type':'integer'}},'required':['a','b'],'additionalProperties':False},'output_schema':{'type':'object','properties':{'sum':{'type':'integer'}},'required':['sum'],'additionalProperties':False}}]},
        'source':"def handle(tool, input, context):\n    return {'sum':input['a']+input['b']}\n",'tests':[{'tool':'add','input':{'a':2,'b':3},'expected':{'sum':5}}]})


async def full(chat,owner):
    await PermissionHTTP(chat,None).change(owner,ModeChange(request_id=uuid4(),expected_revision=1,mode='full'))


def test_plugin_isolated_protocol_tests_auto_enable_but_revocation_wins(world,monkeypatch):
    app=app_for(world,monkeypatch,sandbox=True)
    async def run():
        chat=app.state.chat;chat.plugins.config.enabled=True
        await chat.plugins.seed();await full(chat,world.owner)
        conversation,_,_=await setup(app,world,[])
        tester=chat.plugins.tester
        node=next(iter(chat.harness.link.config.sandbox_nodes))
        tester.transport.status=lambda _: {'available':True}
        async def execute(*args,**kwargs): return {'outcome':'succeeded','output':{'sum':5}}
        tester.transport.execute=execute
        version=await chat.plugins.install(world.owner,conversation['conversation_id'],uuid4(),candidate())
        started=await tester.start(world.owner,conversation['conversation_id'],version['version_id'])
        async with world.db.transaction(write=False) as conn: runrow=await one(conn,'SELECT * FROM plugin_test_runs WHERE test_run_id=?',(started['test_run_id'],))
        await tester.process(runrow,asyncio.Event())
        async with world.db.transaction(write=False) as conn:
            row=await one(conn,'SELECT * FROM plugin_versions WHERE version_id=?',(version['version_id'],))
            assert row['test_status']=='passed' and row['enabled'] and row['approved']
            assert json.loads(row['test_json'])['business_verified'] is False
        changed=candidate().model_dump(mode='json');changed['manifest']['version']='1.0.1'
        version=await chat.plugins.install(world.owner,conversation['conversation_id'],uuid4(),PluginRelease.model_validate(changed))
        started=await tester.start(world.owner,conversation['conversation_id'],version['version_id'])
        async with world.db.transaction() as conn:
            runrow=await one(conn,'SELECT * FROM plugin_test_runs WHERE test_run_id=?',(started['test_run_id'],))
            row=await one(conn,'SELECT * FROM plugin_versions WHERE version_id=?',(version['version_id'],))
            await chat.plugins.decide(conn,world.owner,row,conversation['conversation_id'],'disable')
        assert not await tester.allowed(runrow)
        await tester.process(runrow,asyncio.Event())
        async with world.db.transaction(write=False) as conn:
            row=await one(conn,'SELECT * FROM plugin_versions WHERE version_id=?',(version['version_id'],))
            assert row['test_status']=='interrupted' and not row['enabled']
    asyncio.run(run())


def test_goal_continuation_is_bounded_deduplicated_and_shares_cost(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat;chat.plugins.config.enabled=True
        await chat.plugins.seed();await full(chat,world.owner)
        conversation,_,turn=await setup(app,world,[{'intent':'plugin_execute','arguments':{'plugin_id':'builtin.goal','tool':'create_goal','input':{'objective':'检查已有证据','max_rounds':2}}}])
        for _ in range(4): await chat.process_one()
        await tick(chat.plugins);await tick(chat.plugins)
        async with world.db.transaction(write=False) as conn:
            wakeups=await conn.execute_fetchall('SELECT * FROM plugin_wakeups')
            assert len(wakeups)==1 and wakeups[0]['status']=='submitted'
            remaining=await budget(conn,chat,wakeups[0]['turn_id'],50000)
            assert 0<remaining<50000
        for _ in range(3): await chat.process_one()
        await tick(chat.plugins)
        async with world.db.transaction(write=False) as conn:
            goal=await one(conn,'SELECT * FROM plugin_goals')
            assert goal['rounds']==2 and goal['status']=='paused'
            assert (await one(conn,'SELECT count(*) FROM plugin_wakeups'))[0]==1
        await recover(chat.plugins)
    asyncio.run(run())


@pytest.mark.skipif(os.name!='nt',reason='real Windows Node task and receipt')
def test_plugin_dispatch_reuses_receipts_and_does_not_repeat_effect(world,monkeypatch,tmp_path):
    from nexus.node.config import NodeConfig
    from nexus.node.runtime import NodeRuntime
    from nexus.plugins.config import PluginNodeConfig
    from nexus.core.storage.execution import ExecutionRepository
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat;chat.plugins.config.enabled=True
        await chat.plugins.seed();await full(chat,world.owner)
        policy=PluginNodeConfig(enabled=True)
        app.state.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset({'plugin.call@1'}),roots=frozenset({'workspace'}),plugin_digest=policy.digest())
        for identity in (world.owner_id,world.node_id): await world.security.set_grant(identity,'plugin.call',root_id='workspace')
        conversation,_,turn=await setup(app,world,[{'intent':'plugin_execute','arguments':{'plugin_id':'builtin.files','tool':'write','input':{'path':'actual.txt','content':'真实执行','expected_sha256':None}}}])
        for _ in range(2): await chat.process_one()
        current=await chat.get(world.owner,turn['turn_id'])
        task=await world.tasks.get(UUID(current['work_steps'][0]['task_id']),world.owner_id)
        assert task.status=='ready'
        repo=ExecutionRepository(world.db,world.security,32768)
        command=await repo.dispatch(task.task_id,world.node,app.state.harness.scope(world.node_id),uuid4(),uuid4(),10)
        data=tmp_path/'node';data.mkdir()
        with NodeRuntime(NodeConfig(node_id=world.node_id,data_dir=data,roots={'workspace':tmp_path},allowed_tools=frozenset({'plugin.call@1'}),plugins=policy)) as runtime:
            status=await asyncio.to_thread(runtime.execute,command)
            assert status.phase=='completed',status
            result=status.result
            assert result.outcome=='succeeded'
            repeated=runtime.execute(command)
            assert repeated.phase=='completed'
        await repo.store_result(world.node,result);await repo.verify(command.attempt_id)
        for _ in range(3): await chat.process_one()
        final=await chat.get(world.owner,turn['turn_id'])
        assert final['work_steps'][0]['result']['status']=='succeeded'
        assert (tmp_path/'actual.txt').read_text(encoding='utf-8')=='真实执行'
    asyncio.run(run())
