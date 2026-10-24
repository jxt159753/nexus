"""A routed conversation exports real bytes, then delivers them to another Node."""
import asyncio
import hashlib
import json
import os
import socket
import threading
from uuid import UUID,uuid4

import pytest
import uvicorn
from fastapi.testclient import TestClient
from nexus.core.cognitive import work_catalog
from nexus.core.config import TrustedNode
from nexus.core.models.providers import ModelReply
from nexus.core.storage.database import one
from nexus.core.storage.execution import ExecutionRepository
from nexus.node.config import NodeConfig
from nexus.node.link import NodeClient
from nexus.node.runtime import NodeRuntime
from nexus.node.service_config import NodeServiceConfig
from nexus.plugins.config import PluginNodeConfig
from test_chat_skills import app_for
from test_node_routing import phone,submit
from test_harness_http import login


@pytest.mark.parametrize('case',['valid','other_conversation','revoked','plan'])
def test_chat_send_is_bound_to_conversation_permission_and_plan(world,monkeypatch,case):
    from test_chat_work import setup
    from test_chat_attachments import upload
    from nexus.contracts.chat import ConversationCreate
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat
        app.state.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset({'filesystem.receive@1'}),roots=frozenset({'workspace'}))
        for identity in (world.owner_id,world.node_id):await world.security.set_grant(identity,'filesystem.receive',root_id='workspace')
        conv,grant,turn=await setup(app,world,[])
        other=await chat.create(world.owner,ConversationCreate(request_id=uuid4())) if case=='other_conversation' else conv
        item=await upload(chat,world,other,b'private exact bytes','demo.txt')
        chat.provider.actions=[{'intent':'send_file','arguments':{'source_kind':'attachment','source_id':item['attachment_id']}}]
        await chat.process_one()
        if case=='revoked':await chat.work.revoke(world.owner,conv['conversation_id'])
        if case=='plan':
            async with world.db.transaction() as conn:await conn.execute('UPDATE conversations SET plan_mode=1 WHERE conversation_id=?',(conv['conversation_id'],))
        await chat.process_one()
        async with world.db.transaction(write=False) as conn:
            rows=await conn.execute_fetchall('SELECT * FROM tasks')
            if case=='valid':
                assert len(rows)==1 and rows[0]['status']=='ready'
                args=json.loads(rows[0]['action_json'])['arguments'];assert args['attachment_id']==item['attachment_id'] and args['sha256']==item['sha256']
            else:
                assert not rows or all(r['status']=='failed' for r in rows)
                assert (await one(conn,'SELECT count(*) FROM task_attempts'))[0]==0
    asyncio.run(run())


class Provider:
    def __init__(self,source,target):self.source,self.target,self.calls=source,target,0
    async def invoke(self,messages,receive,**options):
        self.calls+=1
        if self.calls==1:
            node,intent,args=self.source,'plugin_execute',{'plugin_id':'builtin.present','tool':'present','input':{'files':[{'path':'demo.txt'}]}}
        elif self.calls==2:
            data=json.loads(messages[-1]['content'])['data']['files'][0]
            node,intent,args=self.target,'send_file',{'source_kind':'deliverable','source_id':data['deliverable_id']}
        else:return ModelReply('已核对真实文件送达证据。',100,20,True)
        catalog=options['work_catalog'];entry=next(e for e in catalog if e.get('route_node_id')==str(node) and e.get('route_intent')==intent)
        native={'id':'call-'+str(self.calls),'type':'function','function':{'name':entry['name'],'arguments':json.dumps(args)}}
        return ModelReply('',100,20,True,finish_reason='tool_calls',proposal=work_catalog.parse_call(catalog,entry['name'],args),native_tool_call=native)


@pytest.mark.skipif(os.name!='nt',reason='real Windows managed Node integration')
def test_workspace_to_other_node_chat_cards_and_scope(world,monkeypatch,tmp_path):
    app=app_for(world,monkeypatch);raw=(b'actual bytes\r\n\x00\xff'*40000)
    source=tmp_path/'source';source.mkdir();(source/'demo.txt').write_bytes(raw)
    target=tmp_path/'target';target.mkdir()
    async def run():
        chat=app.state.chat;chat.plugins.config.enabled=True;await chat.plugins.seed()
        policy=PluginNodeConfig(enabled=True)
        import secrets
        from pydantic import SecretStr
        token=SecretStr(secrets.token_urlsafe(32));mobile_id=uuid4()
        await world.security.register_identity(mobile_id,'node',token)
        mobile=await world.security.authenticate_token(token,'node')
        app.state.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset({'plugin.call@1'}),roots=frozenset({'workspace'}),plugin_digest=policy.digest())
        app.state.harness.link.config.nodes[mobile.identity_id]=TrustedNode(tools=frozenset({'filesystem.receive@1','android.device_status@1'}),roots=frozenset({'workspace','android:device'}))
        for identity in (world.owner_id,world.node_id):await world.security.set_grant(identity,'plugin.call',root_id='workspace')
        for identity in (world.owner_id,mobile.identity_id):await world.security.set_grant(identity,'filesystem.receive',root_id='workspace')
        provider=Provider(world.node_id,mobile.identity_id);chat.provider=provider
        turn,_,_=await submit(app,world)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',lifespan='off'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        try:
            for _ in range(500):
                if server.started:break
                await asyncio.sleep(.01)
            assert server.started
            repo=ExecutionRepository(world.db,world.security,32768)
            for index,(principal,credential,root,plugins,tools) in enumerate([
                (world.node,world.node_token,source,policy,{'plugin.call@1'}),
                (mobile,token,target,PluginNodeConfig(),{'filesystem.receive@1'})]):
                for _ in range(5):
                    await chat.process_one();current=await chat.get(world.owner,turn['turn_id'])
                    if len(current['work_steps'])>index and current['work_steps'][index]['task_id']:break
                step=current['work_steps'][index];task=await world.tasks.get(UUID(step['task_id']),world.owner_id)
                assert task.node_id==principal.identity_id and task.status=='ready',current
                command=await repo.dispatch(task.task_id,principal,app.state.harness.scope(principal.identity_id),uuid4(),uuid4(),10)
                data=tmp_path/f'node-{index}';data.mkdir()
                cfg=NodeServiceConfig(node_id=principal.identity_id,data_dir=data,roots={'workspace':root},allowed_tools=frozenset(tools),plugins=plugins,local_http=True,core_url=f'ws://127.0.0.1:{port}/ws/v1/node')
                with NodeRuntime(NodeConfig.model_validate_json(cfg.model_dump_json(include=set(NodeConfig.model_fields)))) as runtime:
                    NodeClient(cfg,runtime,credential);status=await asyncio.to_thread(runtime.execute,command)
                assert status.phase=='completed' and status.result.outcome=='succeeded',status
                await repo.store_result(principal,status.result);await repo.verify(command.attempt_id)
            for _ in range(3):await chat.process_one()
            done=await chat.get(world.owner,turn['turn_id'])
            assert provider.calls==3 and len(done['work_steps'])==2,done
            files=done['presentation']['files'];assert len(files)==1 and files[0]['status']=='已送达设备'
            item=files[0];assert item['sha256']==hashlib.sha256(raw).hexdigest()
            saved=target/done['work_steps'][1]['result']['data']['path'];assert saved.read_bytes()==raw
            assert len(list(target.rglob('demo.txt')))==1
            await chat.work.advance();assert len(list(target.rglob('demo.txt')))==1
            with TestClient(app,base_url='http://127.0.0.1:8000') as client:
                assert client.get(item['preview_url']).status_code==401
                auth=login(client,world)
                assert client.get(item['preview_url']).json()['preview']['kind']=='unparsed'
                assert client.get(item['download_url']).content==raw
                assert client.get(item['preview_url'].replace(item['conversation_id'],str(uuid4()))).status_code==404
                assert client.post(item['preview_url'].replace('/preview','/attachment'),json={}).status_code==403
                for _ in range(2):assert client.post(item['preview_url'].replace('/preview','/attachment'),json={},headers=auth).status_code==200
            async with world.db.transaction(write=False) as conn:
                assert (await one(conn,'SELECT count(*) FROM chat_attachments'))[0]==1
        finally:
            server.should_exit=True;await asyncio.to_thread(thread.join,5);assert not thread.is_alive()
    asyncio.run(run())
