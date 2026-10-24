"""Existing Skill controller dispatches new native steps with real Shell output."""

import asyncio
import json
import os
import sys
from uuid import UUID, uuid4

import pytest

from nexus.contracts.extensions import SkillGeneration, SkillStep, ExtensionRequestCreate
from nexus.core.security.types import AccessDenied
from nexus.core.security.modes import PermissionHTTP, ModeChange
from nexus.core.storage.database import one, utc_now
from nexus.core.plugins import mcp
from nexus.plugins.catalog import RELEASES
from test_managed_mcp import environment, seed, execute
from test_managed_mcp import SERVER


def generation(command="Write-Output 'NATIVE-SKILL-实际输出'",*,two=False):
    ids=['install','after'] if two else ['install']
    return SkillGeneration.model_validate_json(json.dumps({'kind':'skill','manifest':{'kind':'skill','capability_id':'user.shell_setup','description':'用于验证实际 Shell 输出和权限绑定的固定流程。',
        'input_schema':{'type':'object','properties':{},'additionalProperties':False},
        'output_schema':{'type':'object','properties':{key:{'type':'object','additionalProperties':True} for key in ids},'required':ids,'additionalProperties':False},
        'verification_profile':'skill_native_steps_v1','dependencies':[]},'markdown':'执行固定命令并保留实际输出。','steps':[
            {'id':key,'tool':'plugin.call','tool_version':1,'plugin_id':'builtin.shell','plugin_tool':'pwsh','plugin_digest':RELEASES['builtin.shell'].digest(),
             'arguments':{'command':command if key=='install' else "Set-Content -LiteralPath 'must-not-run.txt' -Value 'unexpected'"}} for key in ids],
        'tests':[{'name':'contract','input':{},'expected_steps':ids}]}))


async def approved(app,world,model):
    request=ExtensionRequestCreate(request_id=uuid4(),kind='skill',node_id=world.node_id,need='固定宿主 Shell 流程')
    await app.state.extensions.create(world.owner,request)
    async with world.db.transaction() as conn:
        row=await one(conn,'SELECT * FROM extension_requests WHERE request_id=?',(str(request.request_id),))
        made=await app.state.extensions._make_candidate(conn,row,model)
    version=await app.state.extensions.test_version(world.owner,UUID(made['version_id']))
    assert version['test_status']=='passed',version
    assert version['test']['result']['native_execution']=='not_run'
    assert not (app.state.harness.link.config.data_dir/'must-not-run.txt').exists()
    version=await app.state.extensions.approve(world.owner,UUID(made['version_id']),'approve',version['artifact_digest'],UUID(version['test']['test_id']))
    await app.state.extensions.enable(world.owner,UUID(made['version_id']))
    return made['version_id']


def test_native_step_requires_explicit_profile_pinning_and_foreground_shell():
    value=generation().model_dump(mode='json')
    value['manifest']['verification_profile']='skill_steps_v1'
    with pytest.raises(ValueError):SkillGeneration.model_validate_json(json.dumps(value))
    for changes in ({'plugin_digest':'0'*64},{'plugin_tool':'unknown'},{'arguments':{'command':'echo 1','persistent':True}},{'arguments':{'command':'echo 1','run_in_background':True}}):
        step={**generation().steps[0].model_dump(mode='json'),**changes}
        with pytest.raises(ValueError):SkillStep.model_validate_json(json.dumps(step))


def test_shell_completion_requires_exit_and_process_stop_evidence():
    from nexus.core.extensions.native_steps import successful
    for data in ({},{'status':'succeeded','exit_code':0},{'status':'succeeded','exit_code':False,'execution_stopped':True},
                 {'status':'succeeded','exit_code':0,'execution_stopped':False}):
        assert not successful(json.dumps({'tool':'pwsh','data':data}))
    assert successful(json.dumps({'tool':'pwsh','data':{'status':'succeeded','exit_code':0,'execution_stopped':True}}))


@pytest.mark.skipif(os.name!='nt',reason='Windows Shell execution')
@pytest.mark.parametrize('mode',['normal','full'])
def test_skill_dispatches_real_shell_and_keeps_mode_authority(world,tmp_path,mode):
    app,config,_=environment(world,tmp_path)
    async def run():
        cid=await seed(app,world,mode)
        version=await approved(app,world,generation())
        with pytest.raises(AccessDenied):await app.state.skills.start(world.owner,UUID(version),world.node_id,{})
        request=uuid4()
        started=await app.state.skills.start(world.owner,UUID(version),world.node_id,{},request_id=request,conversation_id=cid)
        assert (await app.state.skills.start(world.owner,UUID(version),world.node_id,{},request_id=request,conversation_id=cid))['run_id']==started['run_id']
        await app.state.skills.tick()
        current=await app.state.skills.get(world.owner,UUID(started['run_id']))
        tid=UUID(current['steps'][0]['task_id']);task=await world.tasks.get(tid,world.owner_id)
        assert task.status==('ready' if mode=='full' else 'awaiting_approval')
        if mode=='normal':
            async with world.db.transaction(write=False) as conn:
                approval=await one(conn,'SELECT approval_id FROM approvals WHERE task_id=?',(str(tid),))
            await world.security.decide(UUID(approval[0]),world.owner,'approve',world.scope)
        await execute(world,config,tid);await app.state.skills.tick()
        final=await app.state.skills.get(world.owner,UUID(started['run_id']))
        assert final['status']=='succeeded',final
        assert 'NATIVE-SKILL-实际输出' in final['result']['install']['data']['output']
        assert final['result']['install']['data']['exit_code']==0
    asyncio.run(run())


@pytest.mark.skipif(os.name!='nt',reason='Windows Shell exit code')
def test_nonzero_shell_exit_stops_remaining_skill_steps(world,tmp_path):
    app,config,_=environment(world,tmp_path)
    async def run():
        cid=await seed(app,world)
        version=await approved(app,world,generation('exit 7',two=True))
        started=await app.state.skills.start(world.owner,UUID(version),world.node_id,{},conversation_id=cid)
        await app.state.skills.tick()
        step=(await app.state.skills.get(world.owner,UUID(started['run_id'])))['steps'][0]
        await execute(world,config,step['task_id']);await app.state.skills.tick()
        final=await app.state.skills.get(world.owner,UUID(started['run_id']))
        assert final['status']=='failed' and final['error']['code']=='NATIVE_STEP_FAILED'
        assert final['steps'][1]['task_id'] is None and not (tmp_path/'must-not-run.txt').exists()
    asyncio.run(run())


def test_mode_change_and_plan_mode_do_not_reuse_native_skill_authority(world,tmp_path):
    app,config,_=environment(world,tmp_path)
    async def run():
        cid=await seed(app,world)
        version=await approved(app,world,generation())
        started=await app.state.skills.start(world.owner,UUID(version),world.node_id,{},conversation_id=cid)
        await app.state.skills.tick()
        step=(await app.state.skills.get(world.owner,UUID(started['run_id'])))['steps'][0]
        await PermissionHTTP(app.state.chat,None).change(world.owner,ModeChange(request_id=uuid4(),expected_revision=2,mode='normal'),cid)
        await app.state.skills.tick()
        final=await app.state.skills.get(world.owner,UUID(started['run_id']))
        assert final['status']=='failed' and (await world.tasks.get(UUID(step['task_id']),world.owner_id)).status=='cancelled'
        async with world.db.transaction() as conn:await conn.execute('UPDATE conversations SET plan_mode=1 WHERE conversation_id=?',(str(cid),))
        with pytest.raises(AccessDenied):await app.state.skills.start(world.owner,UUID(version),world.node_id,{},conversation_id=cid)
    asyncio.run(run())


@pytest.mark.skipif(os.name!='nt',reason='Windows Shell/MCP composition')
def test_three_step_skill_installs_registers_and_discovers_real_mcp(world,tmp_path):
    app,config,_=environment(world,tmp_path)
    generated=tmp_path/'generated_server.py'
    model=generation().model_dump(mode='json')
    model['steps'][0]['arguments']['command']="$source=@'\n"+SERVER+"\n'@\n[IO.File]::WriteAllText('"+str(generated)+"',$source,[Text.UTF8Encoding]::new($false))"
    for identifier,name,args in [('register','mcp_register',{'server':'skill_server','expected_revision':0,'configuration':{'process':{'executable':sys.executable,'argv':['-B',str(generated)]}}}),
                                 ('discover','mcp_discover',{'server':'skill_server'})]:
        model['steps'].append({'id':identifier,'tool':'plugin.call','tool_version':1,'plugin_id':'builtin.mcp','plugin_tool':name,
                              'plugin_digest':RELEASES['builtin.mcp'].digest(),'arguments':args})
    ids=['install','register','discover']
    model['manifest']['output_schema']={'type':'object','properties':{key:{'type':'object','additionalProperties':True} for key in ids},'required':ids,'additionalProperties':False}
    model['tests'][0]['expected_steps']=ids
    async def run():
        cid=await seed(app,world)
        version=await approved(app,world,SkillGeneration.model_validate_json(json.dumps(model)))
        assert not generated.exists(),'contract tests must not execute installation code'
        started=await app.state.skills.start(world.owner,UUID(version),world.node_id,{},conversation_id=cid)
        for _ in range(8):
            await app.state.skills.tick()
            current=await app.state.skills.get(world.owner,UUID(started['run_id']))
            if current['status']!='running':break
            step=current['steps'][current['current_step']-1]
            if step['task_id'] and step['status']!='succeeded':
                task=await world.tasks.get(UUID(step['task_id']),world.owner_id)
                if task.status=='ready':await execute(world,config,task.task_id)
        final=await app.state.skills.get(world.owner,UUID(started['run_id']))
        assert final['status']=='succeeded',final
        assert generated.is_file()
        async with world.db.transaction(write=False) as conn:
            row=await mcp.active(conn,world.owner_id,world.node_id,'skill_server')
            assert row['revision']==2 and json.loads(row['catalog_json'])[0]['name']=='add'
            # Completed discovery remains readable after the next revision;
            # it must never become executable again.
            for step in final['steps']:
                task=await world.tasks.get(UUID(step['task_id']),world.owner_id)
                await world.security._policy_eligible(conn,task,world.scope,utc_now(),require_unexpired=False)
    asyncio.run(run())


def test_invalid_discovery_evidence_does_not_advance_skill(world,tmp_path):
    app,config,configuration=environment(world,tmp_path)
    value=generation(two=True).model_dump(mode='json')
    value['steps'][0].update(plugin_id='builtin.mcp',plugin_tool='mcp_register',plugin_digest=RELEASES['builtin.mcp'].digest(),
                             arguments={'server':'bad_evidence','expected_revision':0,'configuration':configuration})
    async def run():
        cid=await seed(app,world)
        version=await approved(app,world,SkillGeneration.model_validate_json(json.dumps(value)))
        started=await app.state.skills.start(world.owner,UUID(version),world.node_id,{},conversation_id=cid)
        await app.state.skills.tick()
        current=await app.state.skills.get(world.owner,UUID(started['run_id']))
        await execute(world,config,current['steps'][0]['task_id'],actual=False,data={
            'release_digest':RELEASES['builtin.mcp'].digest(),'tool':'mcp_register','data':{
                'tools':[],'catalog_digest':'0'*64,'verification':'mcp_tools_discovered'}})
        await app.state.skills.tick()
        final=await app.state.skills.get(world.owner,UUID(started['run_id']))
        assert final['status']=='failed' and final['error']['code']=='MCP_DISCOVERY_NOT_ACTIVATED'
        assert final['steps'][1]['task_id'] is None
    asyncio.run(run())


def test_native_failure_keeps_checked_mcp_reason_and_stops_later_steps(world,tmp_path):
    app,config,configuration=environment(world,tmp_path)
    value=generation(two=True).model_dump(mode='json')
    value['steps'][0].update(plugin_id='builtin.mcp',plugin_tool='mcp_register',plugin_digest=RELEASES['builtin.mcp'].digest(),
                             arguments={'server':'busy_desktop','expected_revision':0,'configuration':configuration})
    evidence={'release_digest':RELEASES['builtin.mcp'].digest(),'tool':'mcp_register','data':{
        'status':'failed','result':{'isError':True,'content':[{'type':'text','text':'{"code":"USER_CONTROL","wait_seconds":10}'}]}}}
    async def run():
        cid=await seed(app,world)
        version=await approved(app,world,SkillGeneration.model_validate_json(json.dumps(value)))
        started=await app.state.skills.start(world.owner,UUID(version),world.node_id,{},conversation_id=cid)
        await app.state.skills.tick()
        step=(await app.state.skills.get(world.owner,UUID(started['run_id'])))['steps'][0]
        task=await execute(world,config,step['task_id'],actual=False,data=evidence)
        assert task.status=='succeeded'  # Transport success is not MCP success.
        await app.state.skills.tick()
        final=await app.state.skills.get(world.owner,UUID(started['run_id']))
        assert final['status']=='failed' and final['verification'] is None
        assert final['error']['code']=='NATIVE_STEP_FAILED'
        assert final['error']['task_id']==step['task_id'] and final['error']['step_id']=='install'
        assert final['error']['tool_result']==task.result.model_dump(mode='json')
        assert final['steps'][0]['status']=='failed' and final['steps'][0]['result']==final['error']['tool_result']
        assert final['steps'][1]['task_id'] is None
        await app.state.skills.tick()
        assert (await app.state.skills.get(world.owner,UUID(started['run_id'])))==final
    asyncio.run(run())


@pytest.mark.skipif(os.name!='nt',reason='Windows native Skill chat result')
def test_chat_plan_native_execution_and_verified_result_preview(world,tmp_path,monkeypatch):
    from nexus.contracts.chat import ChatResultPreview
    from nexus.core.models.config import ModelConfig
    from test_chat_skills import SkillProvider, suggest, confirm
    monkeypatch.setenv('DEEPSEEK_API_KEY','synthetic-native-no-network')
    app,config,_=environment(world,tmp_path,models=ModelConfig(enabled=True,chat_enabled=True))
    app.state.chat.provider=SkillProvider()
    async def run():
        cid=await seed(app,world)
        await approved(app,world,generation())
        _,turn,_=await suggest(app,world,conversation={'conversation_id':str(cid)})
        assert turn['skill_plan']['ready'],turn
        step=turn['skill_plan']['steps'][0]
        assert step['plugin_tool']=='pwsh' and not step['approval_required']
        assert turn['skill_plan']['permission_fingerprint']
        started=(await app.state.chat.decide_action(world.owner,UUID(turn['turn_id']),confirm(turn)))['skill_run']
        await app.state.skills.tick()
        current=await app.state.skills.get(world.owner,UUID(started['run_id']))
        await execute(world,config,current['steps'][0]['task_id'])
        await app.state.skills.tick();await app.state.skills.tick()
        preview=await app.state.chat.preview_result(world.owner,UUID(turn['turn_id']),ChatResultPreview(fields=['install']))
        assert 'NATIVE-SKILL-实际输出' in preview['data']['result']['install']['data']['output']
    asyncio.run(run())
