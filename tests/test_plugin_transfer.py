"""Actual authenticated Node HTTP upload, byte digest and owner download."""

import asyncio
import hashlib
import os
import socket
import threading
import time
from uuid import UUID,uuid4

import pytest
import uvicorn
from fastapi.testclient import TestClient
from nexus.core.config import TrustedNode
from nexus.core.storage.execution import ExecutionRepository
from nexus.node.config import NodeConfig
from nexus.node.service_config import NodeServiceConfig
from nexus.node.runtime import NodeRuntime
from nexus.node.link import NodeClient
from nexus.plugins.config import PluginNodeConfig
from test_chat_skills import app_for
from test_chat_work import setup
from test_plugin_lifecycle import full
from test_node_link import ORIGIN


@pytest.mark.skipif(os.name!='nt',reason='Windows Node integration')
@pytest.mark.parametrize('kind',['present','mcp'])
def test_present_transfers_real_large_bytes_then_unlocks_download(world,monkeypatch,tmp_path,kind):
    app=app_for(world,monkeypatch)
    payload=('实际文件证据\n'*20000).encode()
    if kind=='mcp':
        from test_mcp_images import image_bytes
        payload=image_bytes()
    (tmp_path/'report.txt').write_bytes(payload)
    async def run():
        chat=app.state.chat;chat.plugins.config.enabled=True
        await chat.plugins.seed();await full(chat,world.owner)
        policy=PluginNodeConfig(enabled=True,allow_managed_mcp=True)
        app.state.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset({'plugin.call@1'}),roots=frozenset({'workspace'}),plugin_digest=policy.digest())
        for identity in (world.owner_id,world.node_id): await world.security.set_grant(identity,'plugin.call',root_id='workspace')
        action={'intent':'plugin_execute','arguments':{'plugin_id':'builtin.present','tool':'present','input':{'files':[{'path':'report.txt','description':'真实大文件'}]}}}
        if kind=='mcp':
            import json,sys
            from nexus.core.plugins.mcp_http import MCPHTTP,Register
            from nexus.core.plugins import mcp
            from test_managed_mcp import seed,execute
            from nexus.core.security.types import NodeScope
            # Discover through the real protocol before authorizing the image call.
            source=tmp_path/'image_server.py'
            source.write_text('''import sys,json,base64,pathlib
for line in sys.stdin:
 v=json.loads(line)
 if 'id' not in v:continue
 if v['method']=='initialize':r={'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}
 elif v['method']=='tools/list':r={'tools':[{'name':'Snapshot','inputSchema':{'type':'object','properties':{}}}]}
 else:r={'content':[{'type':'text','text':'actual image'},{'type':'image','mimeType':'image/png','data':base64.b64encode(pathlib.Path('report.txt').read_bytes()).decode()}]}
 print(json.dumps({'jsonrpc':'2.0','id':v['id'],'result':r}),flush=True)
''',encoding='utf-8')
            cid=await seed(app,world)
            keys=frozenset({'plugin.call@1'})
            world.scope=NodeScope(world.node_id,keys,frozenset({'workspace'}),keys,frozenset({'workspace'}),plugin_digest=policy.digest())
            cfg=NodeConfig(node_id=world.node_id,data_dir=tmp_path,roots={'workspace':tmp_path},plugins=policy)
            configuration={'process':{'executable':sys.executable,'argv':['-B',str(source)]}}
            value=Register.model_validate_json(json.dumps({'request_id':str(uuid4()),'conversation_id':str(cid),'node_id':str(world.node_id),'server':'images','expected_revision':0,'configuration':configuration}))
            submitted=await MCPHTTP(chat.plugins,None).submit(world.owner,value,'mcp_register',{'server':'images','expected_revision':0,'configuration':configuration})
            await execute(world,cfg,submitted['task'].task_id)
            await mcp.poll(chat.plugins)
            async with world.db.transaction(write=False) as conn:
                active=await mcp.active(conn,world.owner_id,world.node_id,'images')
            action={'intent':'plugin_execute','arguments':{'plugin_id':'builtin.mcp','tool':'mcp_call_tool','input':{'server':'images','name':'Snapshot','arguments':{},'catalog_digest':active['catalog_digest']}}}
        _,_,turn=await setup(app,world,[action])
        for _ in range(2): await chat.process_one()
        current=await chat.get(world.owner,turn['turn_id'])
        task=await world.tasks.get(UUID(current['work_steps'][0]['task_id']),world.owner_id)
        repo=ExecutionRepository(world.db,world.security,32768)
        command=await repo.dispatch(task.task_id,world.node,app.state.harness.scope(world.node_id),uuid4(),uuid4(),10)
        data=tmp_path/'node';data.mkdir()
        with socket.socket() as port:
            port.bind(('127.0.0.1',0));number=port.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=number,log_level='error',lifespan='off'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        try:
            deadline=time.monotonic()+5
            while not server.started:
                if time.monotonic()>deadline: raise AssertionError('test HTTP server not started')
                await asyncio.sleep(.01)
            config=NodeServiceConfig(node_id=world.node_id,data_dir=data,roots={'workspace':tmp_path},allowed_tools=frozenset({'plugin.call@1'}),plugins=policy,local_http=True,core_url=f'ws://127.0.0.1:{number}/ws/v1/node')
            with NodeRuntime(NodeConfig.model_validate_json(config.model_dump_json(include=set(NodeConfig.model_fields)))) as runtime:
                NodeClient(config,runtime,world.node_token)
                with monkeypatch.context() as worker:
                    if os.environ.get('NEXUS_FILE_NODE_EXE'):
                        import sys
                        worker.setattr(sys,'frozen',True,raising=False)
                        worker.setattr(sys,'executable',os.environ['NEXUS_FILE_NODE_EXE'])
                    status=await asyncio.to_thread(runtime.execute,command)
            assert status.phase=='completed' and status.result.outcome=='succeeded',status
            item=status.result.result.data['files'][0]
            assert item['sha256']==hashlib.sha256(payload).hexdigest() and item['size_bytes']==len(payload)
            client=TestClient(app,base_url=ORIGIN)
            assert client.get(item['download_url']).status_code==401
            login=client.post('/api/v1/session',headers={'origin':ORIGIN},json={'token':world.owner_token.get_secret_value()})
            assert login.status_code==200
            assert client.get(item['download_url']).status_code==404
            await repo.store_result(world.node,status.result);await repo.verify(command.attempt_id)
            for _ in range(3): await chat.process_one()
            current=await chat.get(world.owner,turn['turn_id'])
            assert current['presentation']['files'][0]['sha256']==item['sha256']
            if kind=='mcp':
                preview=client.get(current['presentation']['files'][0]['preview_url'])
                assert preview.status_code==200 and preview.json()['preview']['kind']=='image',preview.text
                assert not list(data.glob('mcp-images-*'))
            response=client.get(item['download_url'])
            assert response.status_code==200 and response.content==payload
            assert response.headers['x-content-sha256']==item['sha256']
            client.close()
        finally:
            server.should_exit=True
            await asyncio.to_thread(thread.join,5)
            assert not thread.is_alive()
    asyncio.run(run())
