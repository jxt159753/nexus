"""Cross-device selection must retain real per-device execution authority."""
import asyncio
import json
import secrets
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from fastapi.testclient import TestClient
from nexus.contracts.chat import ChatSubmission, ConversationCreate
from nexus.core.cognitive import node_routing, work_catalog
from nexus.core.config import TrustedNode
from nexus.core.models.providers import ModelReply
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, utc_now
from nexus.core.storage.execution import ExecutionRepository, RESULT
from test_chat_skills import app_for
from test_chat_work import basic
from test_harness_http import login
from test_android import device_result


def test_default_routing_still_requires_consent_and_keeps_explicit_legacy_scope():
    from pydantic import ValidationError
    base=dict(request_id=uuid4(),expected_revision=1,text='告诉我电脑系统',allow_model=True,allow_tools=True)
    with pytest.raises(ValidationError): ChatSubmission(**base)
    with pytest.raises(ValidationError): ChatSubmission(**base,work_digest='a'*64)
    with pytest.raises(ValidationError): ChatSubmission(**base,allow_work_results=True)
    auto=ChatSubmission(**base,work_digest='a'*64,allow_work_results=True)
    assert auto.all_nodes and auto.node_id is None and auto.workspace_work
    pinned=ChatSubmission(**base,node_id=uuid4())
    assert not pinned.all_nodes and not pinned.workspace_work
    restored=ChatSubmission.model_validate_json(pinned.model_dump_json())
    assert restored==pinned
    plain=ChatSubmission(**{**base,'allow_tools':False})
    assert not plain.all_nodes and not plain.workspace_work


async def phone(app, world):
    identifier = uuid4()
    token = SecretStr(secrets.token_urlsafe(32))
    await world.security.register_identity(identifier, 'node', token)
    principal = await world.security.authenticate_token(token, 'node')
    app.state.harness.link.config.nodes[identifier] = TrustedNode(tools=frozenset({'android.device_status@1'}),roots=frozenset({'android:device'}))
    for identity in (world.owner_id,identifier):
        await world.security.set_grant(identity,'android.device_status',root_id='android:device')
    return principal


class RoutedProvider:
    def __init__(self, targets): self.targets=list(targets); self.calls=[]
    async def invoke(self,messages,on_delta,**options):
        self.calls.append(options)
        if not self.targets: return ModelReply('已查询电脑系统和手机电量。',100,20,True)
        target=self.targets.pop(0)
        node,intent=target[:2]
        arguments=target[2] if len(target)>2 else {}
        catalog=options['work_catalog']
        entry=next(e for e in catalog if e.get('route_node_id')==str(node) and e.get('route_intent')==intent)
        proposal=work_catalog.parse_call(catalog,entry['name'],arguments)
        native={'id':f'route-{len(self.calls)}','type':'function','function':{'name':entry['name'],'arguments':json.dumps(arguments)}}
        return ModelReply('',100,20,True,finish_reason='tool_calls',proposal=proposal,native_tool_call=native)


async def submit(app, world):
    chat=app.state.chat
    conv=await chat.create(world.owner,ConversationCreate(request_id=uuid4()))
    async with world.db.transaction(write=False) as conn:
        directory=await node_routing.directory(chat,conn,world.owner)
    request=ChatSubmission(request_id=uuid4(),expected_revision=conv['revision'],text='告诉我电脑系统和手机电量',allow_model=True,
        allow_tools=True,client_node_id=world.node_id,allow_work_results=True,work_digest=directory['work_digest'])
    assert request.all_nodes and request.workspace_work
    turn=await chat.submit(world.owner,conv['conversation_id'],request)
    return turn,request,directory


def test_routed_skill_can_be_inspected_and_run_on_approved_device(world,monkeypatch):
    from test_chat_skills import prepare
    from test_extensions_m4 import finish_task
    app=app_for(world,monkeypatch)
    async def run():
        mobile=await phone(app,world)
        version=await prepare(app,world)
        chat=app.state.chat
        chat.provider=RoutedProvider([(world.node_id,'inspect_capability',{'version_id':version['version_id']}),
            (world.node_id,'run_skill',{'version_id':version['version_id'],'input':{}})])
        turn,_,directory=await submit(app,world)
        assert work_catalog.capability_snapshot(directory['catalog'],mobile.identity_id,version['version_id'],'run_skill') is None
        assert work_catalog.capability_snapshot(directory['catalog'],world.node_id,version['version_id'],'run_tool') is None
        for _ in range(16):
            await chat.process_one();await app.state.skills.tick()
            current=await chat.get(world.owner,turn['turn_id'])
            for step in current['work_steps']:
                if step.get('skill_run_id'):
                    result=await app.state.skills.get(world.owner,UUID(step['skill_run_id']))
                    for child in result['steps']:
                        if child['task_id'] and child['status']!='succeeded':
                            task=await world.tasks.get(UUID(child['task_id']),world.owner_id)
                            if task.status=='ready':await finish_task(world,task.task_id)
            if current['status']=='completed' and not current['work_status']:break
        done=await chat.get(world.owner,turn['turn_id'])
        assert [s['status'] for s in done['work_steps']]==['succeeded','succeeded']
        assert done['work_steps'][1]['result']['verification']=='skill_steps_verified'
    asyncio.run(run())


def test_two_devices_in_one_turn_use_their_own_authority_and_one_budget(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        await basic(app,world); mobile=await phone(app,world);chat=app.state.chat
        chat.provider=RoutedProvider([(world.node_id,'get_system_info'),(mobile.identity_id,'android_device_status')])
        turn,request,directory=await submit(app,world)
        assert {n['name'] for n in directory['nodes']}=={'我的电脑','我的手机'}
        assert (await chat.submit(world.owner,turn['conversation_id'],request))['turn_id']==turn['turn_id']
        repo=ExecutionRepository(world.db,world.security,32768)
        for index,principal in enumerate((world.node,mobile)):
            for _ in range(2): await chat.process_one()
            current=await chat.get(world.owner,turn['turn_id']); step=current['work_steps'][index]
            task=await world.tasks.get(UUID(step['task_id']),world.owner_id)
            assert task.node_id==principal.identity_id and task.status=='ready'
            command=await repo.dispatch(task.task_id,principal,app.state.harness.scope(principal.identity_id),uuid4(),uuid4(),10)
            output=({'os_name':'Windows','os_version':'11','architecture':'AMD64','python_version':'3.13'} if index==0 else device_result())
            result=RESULT.validate_json(json.dumps({**command.model_dump(mode='json',include={'session_id','task_id','attempt_id','tool','tool_version','command_digest'}),
                'observed_at':utc_now().isoformat(),'outcome':'succeeded','result':output}))
            await repo.store_result(principal,result);await repo.verify(command.attempt_id)
        for _ in range(3): await chat.process_one()
        done=await chat.get(world.owner,turn['turn_id'])
        assert done['status']=='completed' and len(done['work_steps'])==2 and done['model_call_count']==3
        async with world.db.transaction(write=False) as conn:
            grants=await conn.execute_fetchall('SELECT used_operations,parent_grant_id FROM chat_work_grants')
            assert sum(g['used_operations'] for g in grants)==2
            assert len([g for g in grants if g['parent_grant_id']])==1
    asyncio.run(run())


def test_catalog_rejects_wrong_device_and_revocation_stops_routed_work(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        await basic(app,world);mobile=await phone(app,world);chat=app.state.chat
        chat.provider=RoutedProvider([(mobile.identity_id,'android_device_status')])
        turn,_,directory=await submit(app,world)
        entry=next(e for e in directory['catalog'] if e.get('route_intent')=='get_system_info')
        proposal=work_catalog.parse_call(directory['catalog'],entry['name'],{})
        proposal['arguments']['node_id']=str(mobile.identity_id)
        with pytest.raises(ValueError): work_catalog.checked(directory['catalog'],proposal,{'id':'fake','type':'function','function':{'name':entry['name'],'arguments':'{}'}})
        async with world.db.transaction() as conn:
            await conn.execute('UPDATE identities SET revoked_at=? WHERE identity_id=?',(utc_now().isoformat(),str(mobile.identity_id)))
            row=await one(conn,'SELECT * FROM chat_turns WHERE turn_id=?',(turn['turn_id'],))
            with pytest.raises(AccessDenied): await node_routing.validate_turn(chat,conn,row,utc_now())
        await chat.process_one()
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn,'SELECT count(*) FROM tasks'))[0]==0
    asyncio.run(run())


def test_names_are_owner_only_unique_persistent_and_in_model_catalog(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def seed(): await basic(app,world); return await phone(app,world)
    mobile=asyncio.run(seed())
    with TestClient(app,base_url='http://127.0.0.1:8000') as client:
        auth=login(client,world)
        path=f'/api/v1/nodes/{world.node_id}/name'
        assert client.patch(path,json={'name':'书房电脑'}).status_code==403
        assert client.patch(path,json={'name':'书房电脑'},headers=auth).status_code==200
        assert client.patch(f'/api/v1/nodes/{mobile.identity_id}/name',json={'name':'书房电脑'},headers=auth).status_code==409
        nodes=client.get('/api/v1/nodes').json()['nodes']
        assert next(n for n in nodes if n['node_id']==str(world.node_id))['label']=='书房电脑'
        catalog=client.get('/api/v1/chat/work-catalog').json()
        assert any('书房电脑' in e['description'] for e in catalog['catalog'])


def test_approval_cannot_move_to_another_device_and_root_revocation_stops_child(world,monkeypatch):
    from types import SimpleNamespace
    from nexus.core.security import work as permissions
    app=app_for(world,monkeypatch)
    async def run():
        await basic(app,world);await phone(app,world)
        turn,_,_=await submit(app,world)
        async with world.db.transaction() as conn:
            root=await one(conn,'SELECT * FROM chat_work_grants WHERE parent_grant_id IS NULL')
            child=await one(conn,'SELECT * FROM chat_work_grants WHERE parent_grant_id=?',(root['grant_id'],))
            first,second=str(uuid4()),str(uuid4())
            proposal=json.dumps({'intent':'plugin_execute','arguments':{'plugin_id':'builtin.shell'}})
            for index,identifier,grant,decision in [(1,first,root,'approve'),(2,second,child,None)]:
                await conn.execute("INSERT INTO chat_work_steps(step_id,turn_id,step_index,proposal_json,native_json,status,created_at,routed_grant_id,permission_decision) VALUES(?,?,?,?,?,'pending',?,?,?)",
                    (identifier,turn['turn_id'],index,proposal,'{}',utc_now().isoformat(),grant['grant_id'],decision))
            await conn.execute("UPDATE chat_turns SET status='completed',work_status='waiting' WHERE turn_id=?",(turn['turn_id'],))
            step=await one(conn,'SELECT * FROM chat_work_steps WHERE step_id=?',(second,))
            assert await app.state.chat.plugins.permission(conn,step,child,'builtin.shell',SimpleNamespace(effect='execute',name='shell_exec')) is False
            await conn.execute('UPDATE chat_work_grants SET used_operations=19 WHERE grant_id=?',(root['grant_id'],))
            _, scoped=await permissions.allowed_step(conn,second,utc_now())
            assert scoped['node_id']==child['node_id'] and scoped['used_operations']==19 and scoped['max_operations']==20
            await conn.execute('UPDATE chat_work_grants SET revoked_at=? WHERE grant_id=?',(utc_now().isoformat(),root['grant_id']))
            with pytest.raises(AccessDenied): await permissions.allowed_step(conn,second,utc_now())
    asyncio.run(run())


def test_shared_catalog_fits_and_keeps_pinned_plugins_executable(world,monkeypatch):
    from nexus.core.models.chat import request_body
    from test_plugin_lifecycle import full
    app=app_for(world,monkeypatch)
    async def run():
        await basic(app,world);mobile=await phone(app,world);chat=app.state.chat
        policy=chat.harness.link.config.nodes[world.node_id]
        chat.harness.link.config.nodes[world.node_id]=policy.model_copy(update={'tools':policy.tools|{'plugin.call@1'}})
        for identity in (world.owner_id,world.node_id):
            await world.security.set_grant(identity,'plugin.call',root_id='workspace')
        chat.plugins.config.enabled=True
        chat.skills.extensions.config.generation_enabled=True
        chat.config.web_enabled=True
        chat.work.web.provider=object()  # Catalog presence only; never invoked.
        await chat.plugins.seed()
        await full(chat,world.owner)
        chat.provider=RoutedProvider([(world.node_id,'plugin_execute',{'plugin_id':'builtin.todo','tool':'todo_write','input':{'todos':[]}})])
        turn,_,directory=await submit(app,world)
        entries=directory['catalog']
        for common in ('get_current_time','plugin_install','web_search','web_fetch'):
            assert sum(e.get('route_intent')==common for e in entries)==1
        assert any(e.get('plugin_id')=='builtin.todo' and e['digest'] for e in entries)
        assert not any(e.get('route_node_id')==str(mobile.identity_id) and e.get('route_intent')=='plugin_execute' for e in entries)
        assert len(request_body(chat.config,[{'role':'user','content':'查询电脑系统'}],allow_tools=True,work_catalog=entries))<=chat.config.chat_input_bytes
        for _ in range(5): await chat.process_one()
        done=await chat.get(world.owner,turn['turn_id'])
        assert done['work_steps'][0]['result']['verification']=='core_record_snapshot'
        assert done['work_steps'][0]['status']=='succeeded'
    asyncio.run(run())
