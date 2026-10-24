"""Actual Core HTTP/WS and the fixed Windows Node executable, with no model."""
import asyncio,hashlib,json,os,subprocess,sys
from uuid import uuid4
import httpx2
import pytest
from test_link_processes import spawn,stop,wait_for
from test_chat_attachments_browser import _port

@pytest.mark.skipif(os.name!='nt' or not os.environ.get('NEXUS_FILE_NODE_EXE'),reason='fixed Windows Node opt-in')
def test_fixed_windows_node_delivers_binary_once(world,tmp_path):
    async def grants():
        for identity in (world.owner_id,world.node_id):await world.security.set_grant(identity,'filesystem.receive',root_id='workspace')
    asyncio.run(grants());port=_port();origin=f'http://127.0.0.1:{port}'
    core=tmp_path/'core.toml';node=tmp_path/'node.toml';data=tmp_path/'node';data.mkdir();root=tmp_path/'workspace';root.mkdir()
    core.write_text(f'''data_dir={json.dumps(str(world.db.path.parent))}
owner_id="{world.owner_id}"
host="127.0.0.1"
port={port}
local_http=true
origins=["{origin}"]
[nodes."{world.node_id}"]
tools=["filesystem.receive@1"]
roots=["workspace"]
''',encoding='utf-8')
    node.write_text(f'''node_id="{world.node_id}"
data_dir={json.dumps(str(data))}
core_url="ws://127.0.0.1:{port}/ws/v1/node"
local_http=true
allowed_tools=["filesystem.receive@1"]
[roots]
workspace={json.dumps(str(root))}
''',encoding='utf-8')
    cp=np=None
    with (tmp_path/'process.log').open('w+b') as log,httpx2.Client(base_url=origin,trust_env=False,timeout=20) as client:
        try:
            cp=spawn('nexus.core',core,log);wait_for(lambda:client.get('/').status_code==200)
            auth={'origin':origin};login=client.post('/api/v1/session',json={'token':world.owner_token.get_secret_value()},headers=auth);login.raise_for_status();auth['x-csrf-token']=login.json()['csrf_token']
            env={k:v for k,v in os.environ.items() if not k.startswith('NEXUS_')};env['NEXUS_NODE_TOKEN']=world.node_token.get_secret_value()
            np=subprocess.Popen([os.environ['NEXUS_FILE_NODE_EXE'],'--config',str(node)],env=env,stdout=log,stderr=log,stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
            wait_for(lambda:client.get('/api/v1/file-transfers/targets').json()['nodes'][0]['available'],timeout=25)
            conv=client.post('/api/v1/chat/conversations',json={'request_id':str(uuid4())},headers=auth);conv.raise_for_status();cid=conv.json()['conversation']['conversation_id']
            raw=bytes(range(256))*16385;path=f'/api/v1/chat/conversations/{cid}/attachments'
            uploaded=client.post(path,params={'request_id':str(uuid4()),'filename':'跨设备.bin'},content=raw,headers={**auth,'content-type':'application/octet-stream'});uploaded.raise_for_status()
            item=uploaded.json()['attachment'];path+=f"/{item['attachment_id']}/transfers";body={'request_id':str(uuid4()),'node_id':str(world.node_id)}
            sent=client.post(path,json=body,headers=auth);sent.raise_for_status();tid=sent.json()['task']['task_id']
            def settled():
                value=client.get('/api/v1/tasks/'+tid).json()
                return value if value['task']['status'] in {'succeeded','failed','timed_out','cancelled'} else None
            result=wait_for(settled,timeout=25)
            assert result['task']['status']=='succeeded',result
            saved=root/result['task']['result']['path'];assert saved.read_bytes()==raw
            assert result['task']['result']['sha256']==hashlib.sha256(raw).hexdigest()
            assert client.post(path,json=body,headers=auth).json()['task']['task_id']==tid
            assert len(result['attempts'])==1 and len(list(root.rglob('*.bin')))==1
            if os.environ.get('NEXUS_TEST_NODE'):
                from pathlib import Path
                reply=subprocess.run([os.environ['NEXUS_TEST_NODE'],str(Path(__file__).with_name('file_transfer_live_browser.cjs'))],
                    input=json.dumps({'origin':origin,'token':world.owner_token.get_secret_value()}),text=True,encoding='utf-8',capture_output=True,timeout=75)
                assert reply.returncode==0,reply.stderr
                assert json.loads(reply.stdout)['passed']
                assert any(p.read_bytes()==b'\x97'*(2*1024*1024+1) for p in root.rglob('浏览器发送.bin'))
        finally:
            if np is not None and np.poll() is None:
                subprocess.run(['taskkill','/PID',str(np.pid),'/T','/F'],capture_output=True,timeout=10)
            stop(np);stop(cp)
