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
def test_present_transfers_real_large_bytes_then_unlocks_download(world,monkeypatch,tmp_path):
    app=app_for(world,monkeypatch)
    payload=('实际文件证据\n'*20000).encode()
    (tmp_path/'report.txt').write_bytes(payload)
    async def run():
        chat=app.state.chat;chat.plugins.config.enabled=True
        await chat.plugins.seed();await full(chat,world.owner)
        policy=PluginNodeConfig(enabled=True)
        app.state.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset({'plugin.call@1'}),roots=frozenset({'workspace'}),plugin_digest=policy.digest())
        for identity in (world.owner_id,world.node_id): await world.security.set_grant(identity,'plugin.call',root_id='workspace')
        _,_,turn=await setup(app,world,[{'intent':'plugin_execute','arguments':{'plugin_id':'builtin.present','tool':'present','input':{'files':[{'path':'report.txt','description':'真实大文件'}]}}}])
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
            response=client.get(item['download_url'])
            assert response.status_code==200 and response.content==payload
            assert response.headers['x-content-sha256']==item['sha256']
            client.close()
        finally:
            server.should_exit=True
            await asyncio.to_thread(thread.join,5)
            assert not thread.is_alive()
    asyncio.run(run())
