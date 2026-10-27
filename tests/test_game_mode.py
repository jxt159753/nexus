"""Resource veto, actual plugin receipts, and existing Core authorization."""
import asyncio
import threading
import time
from uuid import UUID, uuid4

import pytest

from nexus.plugins import game_mode
from nexus.plugins.catalog import RELEASES
from nexus.plugins.contracts import PluginNodeArguments
from nexus.plugins.node_runtime import PluginRuntime
from nexus.node.config import NodeConfig
from nexus.plugins.config import PluginNodeConfig
from nexus.plugins.schema import checked


def desktop(directory, mode='auto', active=False, age=0):
    preference = game_mode.Preference(mode=mode, request_id=uuid4())
    (directory/'game-mode.json').write_text(preference.model_dump_json(), encoding='utf-8')
    acknowledge(directory, preference, active, age)
    return preference


def acknowledge(directory, preference, active, age=0):
    state = game_mode.DesktopStatus(**preference.model_dump(), active=active, reason='manual',
                                   updated_at_ms=int((time.time()-age)*1000))
    (directory/'game-mode-status.json').write_text(state.model_dump_json(), encoding='utf-8')


def test_new_plugin_has_fixed_mode_contract_and_no_permission_fields():
    tool = RELEASES['builtin.game_mode'].manifest.tools[1]
    for value in ({'mode':'on','permission_mode':'full'}, {'mode':'unknown'}, {'mode':True}):
        with pytest.raises(ValueError): checked(tool.input_schema,value)
    for mode in ('auto','on','off'): checked(tool.input_schema,{'mode':mode})


def test_manual_preference_overrides_auto_and_stale_state_is_not_applied(tmp_path):
    assert not game_mode.blocked(tmp_path)
    desktop(tmp_path, 'auto', True)
    assert game_mode.blocked(tmp_path)
    desktop(tmp_path, 'off', True)
    assert not game_mode.blocked(tmp_path)
    desktop(tmp_path, 'on', False, age=20)
    assert game_mode.blocked(tmp_path)
    assert game_mode.status(tmp_path)['active'] is None
    assert not game_mode.status(tmp_path)['applied']
    with pytest.raises(ValueError,match='Windows'):
        game_mode.execute(tmp_path,'set_mode',{'mode':'off'},time.monotonic()+1,threading.Event())


def test_requested_mode_waits_for_actual_desktop_ack_and_duplicate_does_not_rewrite(tmp_path):
    desktop(tmp_path)
    runtime = PluginRuntime(NodeConfig(node_id=uuid4(),data_dir=tmp_path,roots={'workspace':tmp_path},plugins=PluginNodeConfig(enabled=True)))
    stop = threading.Event()
    def shell():
        while not stop.wait(.01):
            chosen = game_mode.preference(tmp_path)
            if chosen.mode == 'on':
                acknowledge(tmp_path,chosen,True)
                return
    worker=threading.Thread(target=shell);worker.start()
    args=PluginNodeArguments(plugin_id='builtin.game_mode',tool='set_mode',input={'mode':'on'},
        release_digest=RELEASES['builtin.game_mode'].digest(),context_id=uuid4(),invocation_id=uuid4())
    try:
        result=runtime.execute(args,time.monotonic()+5,threading.Event(),'unused')
        assert result.data['active'] and result.data['applied']
        before=(tmp_path/'game-mode.json').read_bytes()
        assert runtime.execute(args,time.monotonic()+5,threading.Event(),'unused')==result
        assert (tmp_path/'game-mode.json').read_bytes()==before
    finally:
        stop.set();worker.join();runtime.close()


def test_saved_request_without_ack_is_reported_pending(tmp_path):
    desktop(tmp_path)
    result=game_mode.execute(tmp_path,'set_mode',{'mode':'on'},time.monotonic()+.1,threading.Event())
    assert result['mode']=='on' and not result['applied']
    assert result['verification']=='not_applied'


@pytest.mark.parametrize('native',['missing','stale','available'])
def test_node_hello_advertises_game_mode_only_with_live_desktop(tmp_path,native):
    import secrets
    from pydantic import SecretStr
    from nexus.node.runtime import NodeRuntime
    from nexus.node.service_config import NodeServiceConfig
    from nexus.node.link import NodeClient
    from nexus.contracts.protocol import parse_message
    cfg=NodeConfig(node_id=uuid4(),data_dir=tmp_path,roots={'workspace':tmp_path},plugins=PluginNodeConfig(enabled=True))
    service=NodeServiceConfig(**cfg.model_dump(),core_url='ws://127.0.0.1:9000/ws/v1/node',local_http=True)
    if native!='missing': desktop(tmp_path,age=20 if native=='stale' else 0)
    sent=[]
    class Socket:
        async def send(self,value): sent.append(parse_message(value))
        async def recv(self): raise EOFError('test handshake complete')
    with NodeRuntime(cfg) as runtime:
        client=NodeClient(service,runtime,SecretStr(secrets.token_urlsafe(32)))
        with pytest.raises(EOFError): asyncio.run(client._connected(Socket()))
    assert sent[0].type=='node.hello'
    assert sent[0].payload.plugin_tools['builtin.game_mode']==(['status','set_mode'] if native=='available' else [])


def test_game_mode_blocks_emulator_operations_and_stops_only_owned_phone(tmp_path):
    runtime=PluginRuntime(NodeConfig(node_id=uuid4(),data_dir=tmp_path,roots={'workspace':tmp_path},plugins=PluginNodeConfig(enabled=True)))
    calls=[]
    class Phone:
        lock=threading.RLock(); phone=object(); stopping=False
        @property
        def managed(self): return self.phone is not None
        def stop(self,*args): calls.append('stop');self.stopping=True
        def status(self,*args): calls.append('status');self.phone=None;return {'status':'stopped'}
        def execute(self,name,*args): calls.append(name);return {'status':'stopped'}
        def close(self): pass
    runtime.emulator=Phone()
    try:
        desktop(tmp_path,'on',True)
        for name in ('start','observe','act','type_text','open_app'):
            with pytest.raises(ValueError,match='游戏模式'):
                runtime.call('builtin.android_emulator',name,{},'test',time.monotonic()+5,threading.Event())
        runtime._apply_game_mode();runtime._apply_game_mode()
        assert calls==['stop','status']
        runtime.call('builtin.android_emulator','status',{},'test',time.monotonic()+5,threading.Event())
        desktop(tmp_path,'off',False)
        runtime._apply_game_mode()
        assert 'start' not in calls
        runtime.call('builtin.android_emulator','start',{},'test',time.monotonic()+5,threading.Event())
        assert calls[-1]=='start'
    finally:runtime.close()


@pytest.mark.parametrize('mode',['normal','full'])
def test_game_mode_uses_harness_permissions_and_verified_node_result(world,tmp_path,mode):
    from test_managed_mcp import environment, seed, execute
    from nexus.contracts.task import PluginNodeRequest
    app,config,_=environment(world,tmp_path)
    desktop(tmp_path)
    async def run():
        cid=await seed(app,world,mode)
        request=PluginNodeRequest(request_id=uuid4(),intent='execute_plugin',arguments=PluginNodeArguments(
            plugin_id='builtin.game_mode',tool='status',input={},
            release_digest=RELEASES['builtin.game_mode'].digest(),context_id=cid,invocation_id=uuid4()))
        task=await app.state.harness.submit(world.owner,world.node_id,request)
        if task.status=='awaiting_approval':
            from nexus.core.storage.database import one
            async with world.db.transaction(write=False) as conn:
                approval=await one(conn,'SELECT approval_id FROM approvals WHERE task_id=?',(str(task.task_id),))
            await world.security.decide(UUID(approval[0]),world.owner,'approve',world.scope)
        result=await execute(world,config,task.task_id)
        assert result.status=='succeeded' and result.execution_outcome=='known'
        assert result.result.data['applied']
    asyncio.run(run())


def test_conversation_routes_game_mode_to_actual_plugin_receipt(world,tmp_path,monkeypatch):
    from nexus.contracts.chat import ChatSubmission
    from nexus.core.models.config import ModelConfig
    from nexus.core.cognitive import node_routing
    from test_managed_mcp import environment, seed, execute
    from test_node_routing import RoutedProvider
    monkeypatch.setenv('DEEPSEEK_API_KEY','synthetic-no-network')
    app,config,_=environment(world,tmp_path,models=ModelConfig(enabled=True,chat_enabled=True))
    desktop(tmp_path,'off',False)
    async def run():
        cid=await seed(app,world)
        chat=app.state.chat
        chat.provider=RoutedProvider([(world.node_id,'plugin_inspect',{'plugin_id':'builtin.game_mode'}),
            (world.node_id,'plugin_execute',{'plugin_id':'builtin.game_mode','tool':'set_mode','input':{'mode':'on'}})])
        async with world.db.transaction(write=False) as conn:
            directory=await node_routing.directory(chat,conn,world.owner)
        history=await chat.history(world.owner,cid)
        turn=await chat.submit(world.owner,cid,ChatSubmission(request_id=uuid4(),expected_revision=history['conversation']['revision'],
            text='打开电脑游戏模式',allow_model=True,allow_tools=True,client_node_id=world.node_id,
            allow_work_results=True,work_digest=directory['work_digest']))
        executed=set()
        for _ in range(12):
            await chat.process_one()
            current=await chat.get(world.owner,turn['turn_id'])
            for step in current['work_steps']:
                if step.get('task_id') and step['task_id'] not in executed:
                    stop=threading.Event()
                    def shell():
                        while not stop.wait(.01):
                            chosen=game_mode.preference(tmp_path)
                            if chosen.mode=='on':
                                acknowledge(tmp_path,chosen,True)
                                return
                    worker=threading.Thread(target=shell);worker.start()
                    try: task=await execute(world,config,step['task_id'])
                    finally: stop.set();worker.join()
                    assert task.status=='succeeded' and task.result.data['active'] and task.result.data['applied']
                    executed.add(step['task_id'])
            if current['status']=='completed' and not current.get('work_status'):break
        assert current['status']=='completed' and len(executed)==1
        assert [step['status'] for step in current['work_steps']]==['succeeded','succeeded']
    asyncio.run(run())
