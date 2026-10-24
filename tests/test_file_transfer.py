import asyncio
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from nexus.contracts.file_transfer import FileReceiveArguments, FileReceiveCall, FileReceiveResult, receiving_path
from nexus.contracts.messages import CommandResult, NodeHello
from nexus.contracts.protocol import parse_message
from nexus.contracts.tools import validate_result
from nexus.core.config import TrustedNode
from nexus.link import wire_message
from nexus.node.config import NodeConfig
from nexus.node.tools.receive import receive_file
from test_chat import app_for, conversation
from test_chat_attachments import upload
from test_harness_http import login
from test_node_link import headers


TOOL = 'filesystem.receive'


def setup(world,monkeypatch,granted=True):
    app = app_for(world,monkeypatch)
    async def prepare():
        chat=app.state.chat
        conv=await conversation(chat,world)
        item=await upload(chat,world,conv,bytes(range(256))*16385,'测试文件.bin')
        if granted:
            for identity in (world.owner_id,world.node_id):
                await world.security.set_grant(identity,TOOL,root_id='workspace')
        return item
    item=asyncio.run(prepare())
    app.state.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset({TOOL+'@1'}),roots=frozenset({'workspace'}))
    return app,item


def endpoint(item):
    return f"/api/v1/chat/conversations/{item['conversation_id']}/attachments/{item['attachment_id']}/transfers"


def test_transfer_real_http_ws_binary_evidence_idempotency_and_source_retention(world,monkeypatch):
    app,item=setup(world,monkeypatch)
    with TestClient(app,base_url='http://127.0.0.1:8000') as client:
        auth=login(client,world)
        with client.websocket_connect('ws://127.0.0.1:8000/ws/v1/node',headers=headers(world)) as ws:
            ws.send_text(wire_message('node.hello',NodeHello(node_id=world.node_id,node_version='transfer-test',boot_id=uuid4(),tools=[{'tool':TOOL,'tool_version':1}],roots=['workspace'])))
            parse_message(ws.receive_text())
            target=client.get('/api/v1/file-transfers/targets').json()['nodes'][0]
            assert target['available']
            request={'request_id':str(uuid4()),'node_id':str(world.node_id)}
            assert client.post(endpoint(item),json=request).status_code==403
            task=client.post(endpoint(item),json=request,headers=auth).json()['task']
            command=parse_message(ws.receive_text()).payload
            assert task['task_id']==str(command.task_id)
            url=f'/api/v1/file-transfers/{command.attempt_id}/content'
            assert client.get(url).status_code==401
            assert client.get(url,headers={'Authorization':'Bearer '+world.owner_token.get_secret_value()}).status_code==401
            response=client.get(url,headers=headers(world));assert response.status_code==200
            assert len(response.content)==item['size_bytes'] and hashlib.sha256(response.content).hexdigest()==item['sha256']
            assert client.request('DELETE',endpoint(item).removesuffix('/transfers'),json={},headers=auth).status_code==403
            result=TypeAdapter(CommandResult).validate_json(json.dumps({
                **command.model_dump(mode='json',include={'session_id','task_id','attempt_id','command_digest','tool','tool_version'}),
                'observed_at':datetime.now(UTC).isoformat(),'outcome':'succeeded',
                'result':{'root_id':'workspace','attachment_id':item['attachment_id'],'path':command.arguments.path,'sha256':item['sha256'],'bytes_written':item['size_bytes']}}))
            ws.send_text(wire_message('command.result',result))
            assert parse_message(ws.receive_text()).type=='result.ack'
            assert client.get(url,headers=headers(world)).status_code==403
            repeated=client.post(endpoint(item),json=request,headers=auth).json()['task']
            assert repeated['task_id']==task['task_id'] and repeated['status']=='succeeded'
            assert len(client.get(endpoint(item)).json()['tasks'])==1
            assert not app.state.chat.provider.requests


def test_transfer_requires_independent_grants(world,monkeypatch):
    app,item=setup(world,monkeypatch,False)
    with TestClient(app,base_url='http://127.0.0.1:8000') as client:
        auth=login(client,world)
        assert client.get('/api/v1/file-transfers/targets').json()['nodes'][0]['reason']=='not_authorized'
        task=client.post(endpoint(item),json={'request_id':str(uuid4()),'node_id':str(world.node_id)},headers=auth).json()['task']
        assert task['status']=='failed'
        assert not client.get('/api/v1/tasks/'+task['task_id']).json()['attempts']


def test_old_node_not_advertising_receive_is_not_available(world,monkeypatch):
    app,item=setup(world,monkeypatch)
    with TestClient(app,base_url='http://127.0.0.1:8000') as client:
        login(client,world)
        with client.websocket_connect('ws://127.0.0.1:8000/ws/v1/node',headers=headers(world)) as ws:
            ws.send_text(wire_message('node.hello',NodeHello(node_id=world.node_id,node_version='old-node',boot_id=uuid4(),tools=[{'tool':'system.info','tool_version':1}],roots=['workspace'])))
            ws.receive_text()
            target=client.get('/api/v1/file-transfers/targets').json()['nodes'][0]
            assert target['online'] and not target['available'] and target['reason']=='upgrade_required'


def test_transfer_rejects_forged_source_and_cross_conversation(world,monkeypatch):
    app,item=setup(world,monkeypatch)
    with TestClient(app,base_url='http://127.0.0.1:8000') as client:
        auth=login(client,world); rid=uuid4()
        arguments={'root_id':'workspace','conversation_id':item['conversation_id'],'attachment_id':item['attachment_id'],
            'path':receiving_path(rid,item['filename']),'sha256':'0'*64,'size_bytes':item['size_bytes']}
        request={'node_id':str(world.node_id),'request':{'request_id':str(rid),'intent':'receive_file','arguments':arguments}}
        assert client.post('/api/v1/tasks',json=request,headers=auth).status_code==409
        assert client.post(endpoint(item).replace(item['conversation_id'],str(uuid4())),json={'request_id':str(rid),'node_id':str(world.node_id)},headers=auth).status_code==404


@pytest.mark.parametrize('raw',[b'',bytes(range(256))*16385],ids=['empty','binary-4MiB'])
def test_receiver_atomic_create_only_readback_and_digest(tmp_path,raw):
    from types import SimpleNamespace
    rid,attempt=uuid4(),uuid4();data=tmp_path/'data';root=tmp_path/'workspace';data.mkdir();root.mkdir()
    args=FileReceiveArguments(root_id='workspace',conversation_id=uuid4(),attachment_id=uuid4(),path=receiving_path(rid,'测试.bin'),sha256=hashlib.sha256(raw).hexdigest(),size_bytes=len(raw))
    command=SimpleNamespace(arguments=args,attempt_id=attempt)
    config=NodeConfig(node_id=uuid4(),data_dir=data,roots={'workspace':root},allowed_tools=frozenset({TOOL+'@1'}))
    (data/f'receive-{attempt}.tmp').write_bytes(raw)
    result=receive_file(command,config)
    assert (root/args.path).read_bytes()==raw
    validate_result(FileReceiveCall(tool=TOOL,tool_version=1,arguments=args),result,32768)
    with pytest.raises(FileExistsError): receive_file(command,config)
    assert (root/args.path).read_bytes()==raw
    with pytest.raises(ValueError):
        validate_result(FileReceiveCall(tool=TOOL,tool_version=1,arguments=args),result.model_copy(update={'sha256':'0'*64}),32768)


@pytest.mark.parametrize('name',['../a.exe','CON','a:b?.txt','..','很'*200+'.bin'])
def test_portable_safe_receiving_names(name):
    path=receiving_path(uuid4(),name)
    assert len(path.encode())<=240
    FileReceiveArguments(root_id='workspace',conversation_id=uuid4(),attachment_id=uuid4(),path=path,sha256='a'*64,size_bytes=0)


@pytest.mark.skipif(__import__('os').name != 'nt', reason='real Windows Job Object worker')
@pytest.mark.parametrize('mode',['success','bad_hash','cancel'])
def test_real_windows_download_worker_and_receipt_replay(tmp_path,mode):
    import threading, time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from pydantic import SecretStr
    from node_support import sign
    from nexus.contracts.messages import FileReceiveExecute
    from nexus.node.file_transfer import downloader
    from nexus.node.service_config import NodeServiceConfig
    from nexus.node.runtime import NodeRuntime
    raw=bytes(range(256))*16385;digest=hashlib.sha256(raw).hexdigest();started=threading.Event();calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            calls.append(self.path);assert self.headers['Authorization']=='Bearer '+'x'*43
            self.send_response(200);self.send_header('X-Content-SHA256',digest if mode!='bad_hash' else '0'*64);self.end_headers()
            started.set()
            try:
                for offset in range(0,len(raw),65536):
                    self.wfile.write(raw[offset:offset+65536]);self.wfile.flush()
                    if mode=='cancel':time.sleep(.05)
            except (OSError,ConnectionError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    root=tmp_path/'workspace';root.mkdir();data=tmp_path/'data';data.mkdir()
    config=NodeConfig(node_id=uuid4(),data_dir=data,roots={'workspace':root},allowed_tools=frozenset({TOOL+'@1'}))
    service=NodeServiceConfig(**config.model_dump(),core_url=f'ws://127.0.0.1:{server.server_port}/ws/v1/node',local_http=True)
    args=FileReceiveArguments(root_id='workspace',conversation_id=uuid4(),attachment_id=uuid4(),path=receiving_path(uuid4(),'真实进程.bin'),sha256=digest,size_bytes=len(raw))
    command=sign(FileReceiveExecute(tool=TOOL,tool_version=1,arguments=args,session_id=uuid4(),task_id=uuid4(),attempt_id=uuid4(),command_digest='0'*64,grant_id=uuid4(),expires_at=datetime.now(UTC)+timedelta(minutes=1),execution_timeout_seconds=10),config.node_id)
    try:
        with NodeRuntime(config) as runtime:
            runtime.file_downloader=downloader(service,SecretStr('x'*43))
            with ThreadPoolExecutor(1) as pool:
                pending=pool.submit(runtime.execute,command)
                if mode=='cancel':
                    assert started.wait(5)
                    runtime.cancel(command.attempt_id)
                result=pending.result(timeout=15)
            if mode=='success':
                assert result.phase=='completed' and result.result.outcome=='succeeded'
                assert (root/args.path).read_bytes()==raw
                assert runtime.execute(command)==result
            elif mode=='cancel': assert result.phase=='cancelled' and not (root/args.path).exists()
            else: assert result.phase=='completed' and result.result.outcome=='failed' and not (root/args.path).exists()
        assert len(calls)==1 and not list(data.glob('receive-*.tmp'))
    finally:server.shutdown();server.server_close();thread.join(3)
