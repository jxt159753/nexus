"""Screen contracts, real Harness transport and opt-in image projection."""
import asyncio,base64,hashlib,io,json
from datetime import UTC,datetime
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError,TypeAdapter
from nexus.contracts.android_screen import *
from nexus.contracts.images import validate_image_messages
from nexus.contracts.messages import CommandResult,NodeHello
from nexus.contracts.protocol import parse_message
from nexus.contracts.tools import validate_result
from nexus.core.security.service import has_grant
from nexus.core.cognitive.work_images import append_batch_images,model_result
from nexus.link import wire_message
from test_node_link import headers
from test_harness_http import login
from test_device_enrollment import binding,say
import test_android_control as phone


def screen(action=None,prior=None):
    from PIL import Image
    image=Image.new('RGB',(1080,1500),'white');out=io.BytesIO();image.save(out,'PNG');raw=out.getvalue()
    raster=dict(media_type='image/png',width=1080,height=1500,size_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),data_base64=base64.b64encode(raw).decode())
    state=dict(package_name='test.phone.app',window_id=9,width=1080,height=1920,rotation=0,region=[0,0,1080,1920])
    return dict(state=state,image=raster,observation_digest=digest(dict(state=state,image_sha256=raster['sha256'])),
                action=action,previous_digest=prior,verification='gesture_completed' if action else 'screen_observed')


@pytest.mark.parametrize('patch',[{'x':True},{'x':1600},{'y':-1},{'action':'swipe'}, {'end_x':5},{'action':'swipe','end_x':1,'end_y':2}])
def test_screen_rejects_unbound_coordinates(patch):
    with pytest.raises(ValidationError): AndroidScreenActArguments.model_validate(dict(action='tap',observation_digest='a'*64,x=1,y=2)|patch)


def test_screen_evidence_and_crop_binding():
    value=screen();result=AndroidScreenResult(**value)
    command=AndroidScreenObserveCall(tool='android.screen_observe',tool_version=1,arguments={})
    assert validate_result(command,result,32768)==result
    with pytest.raises(ValidationError): AndroidScreenResult(**(value|{'observation_digest':'f'*64}))
    with pytest.raises(ValueError): validate_result(command.model_copy(update={'arguments':AndroidScreenObserveArguments(region=[0,0,100,100])}),result,32768)
    acted=AndroidScreenResult(**screen('tap',value['observation_digest']))
    action=AndroidScreenActCall(tool='android.screen_act',tool_version=1,arguments=dict(action='tap',observation_digest=value['observation_digest'],x=20,y=20))
    validate_result(action,acted,32768)
    with pytest.raises(ValueError): validate_result(action,acted.model_copy(update={'previous_digest':'f'*64}),32768)


def test_screen_real_ws_receipt_and_opt_in(world,monkeypatch):
    monkeypatch.setattr(phone,'ROOTS',{'android.screen_observe':'android:ui','android.screen_act':'android:ui'})
    app=phone.setup(world)
    with TestClient(app,base_url='http://127.0.0.1:8000') as client:
        auth=login(client,world)
        with client.websocket_connect('ws://127.0.0.1:8000/ws/v1/node',headers=headers(world)) as ws:
            ws.send_text(wire_message('node.hello',NodeHello(node_id=world.node_id,node_version='screen-test',boot_id=uuid4(),tools=[{'tool':t,'tool_version':1} for t in phone.ROOTS],roots=['android:ui'])))
            session=parse_message(ws.receive_text()).payload.session_id
            def catalog(): return {e['name'] for e in client.get('/api/v1/chat/work-catalog',params={'node_id':str(world.node_id)}).json()['catalog']}
            assert 'android_screen_observe' not in catalog()
            app.state.chat.config.chat_images_enabled=True
            assert {'android_screen_observe','android_screen_act'}<=catalog()
            observed=screen()
            for tool,args,result in [('android.screen_observe',{},observed),('android.screen_act',dict(action='tap',observation_digest=observed['observation_digest'],x=50,y=60),screen('tap',observed['observation_digest']))]:
                request=dict(node_id=str(world.node_id),request=dict(request_id=str(uuid4()),intent=tool.replace('.','_'),arguments=args))
                created=client.post('/api/v1/tasks',json=request,headers=auth);created.raise_for_status();task=created.json()['task']
                detail=client.get('/api/v1/tasks/'+task['task_id']).json()
                assert task['status']=='awaiting_approval'
                client.post('/api/v1/approvals/'+detail['approvals'][0]['approval_id'],json={'decision':'approve'},headers=auth).raise_for_status()
                command=parse_message(ws.receive_text()).payload
                payload=TypeAdapter(CommandResult).validate_json(json.dumps(dict(session_id=str(session),task_id=str(command.task_id),attempt_id=str(command.attempt_id),tool=tool,tool_version=1,command_digest=command.command_digest,observed_at=datetime.now(UTC).isoformat(),outcome='succeeded',result=result)))
                ws.send_text(wire_message('command.result',payload));assert parse_message(ws.receive_text()).type=='result.ack'
                assert client.get('/api/v1/tasks/'+task['task_id']).json()['task']['status']=='succeeded'
                assert client.post('/api/v1/tasks',json=request,headers=auth).json()['task']['task_id']==task['task_id']


def test_vision_permissions_are_separate_and_revocable(world):
    app=phone.setup(world)
    async def run():
        chat=app.state.chat;device=binding();await chat.devices.prepare(world.owner,device)
        await say(chat,world,'将此设备注册为节点，允许无障碍操作',device)
        state=await chat.devices.status(world.owner,device.client_id,device.proof.get_secret_value())
        assert not state['permissions']['vision']
        await say(chat,world,'允许此设备的屏幕视觉操作权限',device)
        async with world.db.transaction(write=False) as conn:
            assert await has_grant(conn,world.owner_id,device.node_id,'android.screen_observe',1,'android:ui')
            assert not await has_grant(conn,world.owner_id,world.node_id,'android.screen_observe',1,'android:ui')
        await say(chat,world,'关闭此设备的屏幕视觉操作权限',device)
        assert not (await chat.devices.status(world.owner,device.client_id,device.proof.get_secret_value()))['permissions']['vision']
    asyncio.run(run())


def test_screen_images_reach_model_but_not_text_and_long_loop_keeps_four():
    value=screen();data=dict(status='succeeded',data=value)
    assert 'data_base64' not in model_result(data,is_image=True)['data']['image']
    class Conn:
        async def execute_fetchall(self,*args):
            return [dict(native_json=json.dumps({'id':'latest'}),proposal_json=json.dumps({'intent':'android_screen_observe'}),result_json=json.dumps(data))]
    messages=[]
    for _ in range(7):
        messages.append({'role':'assistant','tool_calls':[{'id':'latest'}]})
        asyncio.run(append_batch_images(Conn(),'turn',messages))
    assert validate_image_messages(messages,True)==4
    with pytest.raises(ValueError): validate_image_messages(messages,False)
    assert messages[-1]['content'][-1]['image_url']['url'].endswith(value['image']['data_base64'])


@pytest.mark.parametrize('mode',['normal','full','plan'])
def test_screen_actions_keep_normal_approval_and_plan_read_only(world,monkeypatch,mode):
    from test_chat_skills import app_for
    from test_chat_work import setup as work_setup
    from nexus.core.config import TrustedNode
    from nexus.core.security.modes import ModeChange,PermissionHTTP
    from nexus.core.storage.database import one
    app=app_for(world,monkeypatch);app.state.chat.config.chat_images_enabled=True
    async def run():
        tool='android.screen_act'
        app.state.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset({tool+'@1'}),roots=frozenset({'android:ui'}))
        for identity in (world.owner_id,world.node_id): await world.security.set_grant(identity,tool,root_id='android:ui')
        if mode=='full': await PermissionHTTP(app.state.chat,None).change(world.owner,ModeChange(request_id=uuid4(),expected_revision=1,mode='full'))
        conv,_,turn=await work_setup(app,world,[dict(intent='android_screen_act',arguments=dict(action='tap',observation_digest='a'*64,x=10,y=20))])
        if mode=='plan':
            async with world.db.transaction() as conn: await conn.execute('UPDATE conversations SET plan_mode=1 WHERE conversation_id=?',(conv['conversation_id'],))
        await app.state.chat.process_one();await app.state.chat.process_one()
        async with world.db.transaction(write=False) as conn:
            tasks=await conn.execute_fetchall('SELECT status FROM tasks')
            if mode=='plan': assert not tasks or all(r[0]=='failed' for r in tasks)
            else: assert [r[0] for r in tasks]==['ready' if mode=='full' else 'awaiting_approval']
            assert not await one(conn,'SELECT 1 FROM task_attempts')
    asyncio.run(run())
