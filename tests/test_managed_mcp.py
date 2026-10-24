"""Real stdio protocol plus Core version, consent and recovery boundaries."""

import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from nexus.contracts.chat import ConversationCreate
from nexus.core.app import create_app
from nexus.core.config import ExtensionConfig, TrustedNode
from nexus.core.plugins import mcp
from nexus.core.plugins.mcp_http import MCPHTTP, Register, Operation
from nexus.core.security.modes import ModeChange, PermissionHTTP
from nexus.core.security.types import NodeScope, AccessDenied
from nexus.core.storage.database import one, utc_now
from nexus.core.storage.execution import ExecutionRepository, RESULT
from nexus.plugins.config import PluginConfig, PluginNodeConfig
from nexus.node.config import NodeConfig
from nexus.plugins.node_runtime import PluginRuntime
from nexus.plugins.contracts import PluginNodeArguments
from nexus.plugins.catalog import RELEASES
from nexus.plugins.mcp_contracts import MCPConfiguration, catalog
from test_node_link import core_config, ORIGIN


SERVER='''import json,sys
from pathlib import Path
for line in sys.stdin:
 request=json.loads(line)
 if 'id' not in request:continue
 method=request['method']
 if method=='initialize':result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'actual-test','version':'1'}}
 elif method=='tools/list':
  changed=Path('changed').exists()
  result={'tools':[{'name':'add','description':'changed' if changed else 'integer addition','inputSchema':{'type':'object','properties':{'a':{'type':'integer'},'b':{'type':'integer'}},'required':['a','b'],'additionalProperties':False},'outputSchema':{'type':'object','properties':{'sum':{'type':'integer'}},'required':['sum'],'additionalProperties':False}}]}
 elif method=='tools/call':
  Path('called').write_text('actual call')
  value=request['params']['arguments'];result={'content':[{'type':'text','text':str(value['a']+value['b'])}],'structuredContent':{'sum':value['a']+value['b']},'isError':False}
 else:result={'resources':[]}
 print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
'''


def environment(world,tmp_path,**overrides):
    server=tmp_path/'server.py';server.write_text(SERVER,encoding='utf-8')
    config=NodeConfig(node_id=world.node_id,data_dir=tmp_path,roots={'workspace':tmp_path},
        plugins=PluginNodeConfig(enabled=True,allow_managed_mcp=True,pwsh=Path('C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe') if os.name=='nt' else None))
    keys=frozenset({'plugin.call@1'})
    world.scope=NodeScope(world.node_id,keys,frozenset({'workspace'}),keys,frozenset({'workspace'}),plugin_digest=config.plugin_digest())
    app=create_app(core_config(world,plugins=PluginConfig(enabled=True),extensions=ExtensionConfig(enabled=True,fixture_generation=True),
        nodes={world.node_id:TrustedNode(tools=keys,roots=frozenset({'workspace'}),plugin_digest=config.plugin_digest())},**overrides))
    return app,config,{'process':{'executable':sys.executable,'argv':['-B',str(server)]}}


async def seed(app,world,mode='full'):
    for identity in (world.owner_id,world.node_id):await world.security.set_grant(identity,'plugin.call',root_id='workspace')
    await app.state.plugins.seed()
    conv=await app.state.chat.create(world.owner,ConversationCreate(request_id=uuid4()))
    if mode=='full':
        await PermissionHTTP(app.state.chat,None).change(world.owner,ModeChange(request_id=uuid4(),expected_revision=1,mode='full'),UUID(conv['conversation_id']))
    return UUID(conv['conversation_id'])


def test_model_inspection_explains_unregistered_and_pending_servers(world,tmp_path):
    app,config,configuration=environment(world,tmp_path)
    async def run():
        conversation=await seed(app,world)
        async with world.db.transaction() as conn:
            missing=await mcp.model_list(conn,world.owner_id,world.node_id,'desktop')
            assert missing['state']=='not_registered' and missing['revision']==0 and not missing['tools']
            await mcp.prepare(conn,world.owner_id,world.node_id,conversation,uuid4(),'mcp_register',
                {'server':'desktop','expected_revision':0,'configuration':configuration})
            pending=await mcp.model_list(conn,world.owner_id,world.node_id,'desktop')
            assert pending['state']=='pending' and pending['latest_revision']==1 and not pending['tools']
            with pytest.raises(AccessDenied):await mcp.active(conn,world.owner_id,world.node_id,'desktop')
            assert (await mcp.model_list(conn,uuid4(),world.node_id,'desktop'))['state']=='not_registered'
    asyncio.run(run())


async def execute(world,config,task_id,*,actual=True,data=None):
    task=await world.tasks.get(UUID(str(task_id)),world.owner_id)
    repo=ExecutionRepository(world.db,world.security,32768)
    command=await repo.dispatch(task.task_id,world.node,world.scope,uuid4(),uuid4(),10)
    assert command is not None
    if actual:
        runtime=PluginRuntime(config)
        try: data=runtime.execute(command.arguments,time.monotonic()+10,threading.Event(),'unused').model_dump(mode='json')
        finally:runtime.close()
    values=command.model_dump(mode='json',include={'session_id','task_id','attempt_id','tool','tool_version','command_digest'})
    result=RESULT.validate_json(json.dumps({**values,'observed_at':utc_now().isoformat(),'outcome':'succeeded','result':data}))
    await repo.store_result(world.node,result);await repo.verify(command.attempt_id)
    return await world.tasks.get(task.task_id,world.owner_id)


def test_mcp_configuration_and_conservative_schema():
    for config in ({},{'url':'https://example.test/mcp','process':{'executable':'/usr/bin/python','argv':[]}},
                   {'url':'http://example.test/mcp'},{'url':'https://user:password@example.test/mcp'}, {'process':{'executable':'relative.exe'}}):
        with pytest.raises(ValueError):MCPConfiguration.model_validate({'server':'test','expected_revision':0,'configuration':config})
    value=MCPConfiguration.model_validate({'server':'test','expected_revision':0,'configuration':{'url':'http://127.0.0.1:1234/mcp'}})
    assert value.configuration.url
    tools,digest=catalog([{'name':'unsupported','inputSchema':{'$ref':'https://example.invalid/schema'}}])
    assert not tools[0]['supported'] and len(digest)==64


def test_mcp_windows_path_has_platform_independent_wire_identity():
    from nexus.plugins.mcp_contracts import MCPBinding, server_value
    from nexus.plugins.config import MCPServer
    from nexus.contracts.extensions import digest_value
    values=[]
    for path in ('C:/workspace/mcp/python.exe', r'C:\workspace\mcp\python.exe'):
        config=MCPServer.model_validate_json(json.dumps({'process':{'executable':path,'argv':['serve']}}))
        bound=MCPBinding(registration_id=UUID(int=1),server='desktop',revision=1,
            configuration=config,configuration_digest=digest_value(server_value(config)),
            permission_fingerprint='a'*64,cancel_cursor=0)
        values.append(bound.model_dump(mode='json'))
        assert values[-1]['configuration']['process']['executable']=='C:/workspace/mcp/python.exe'
        assert MCPBinding.model_validate_json(bound.model_dump_json()).model_dump(mode='json')==values[-1]
    assert values[0]==values[1]


def test_fastmcp_wrapper_annotation_preserves_output_validation():
    from nexus.plugins.schema import checked
    tools,_=catalog([{'name':'Process','inputSchema':{'type':'object'},
        'outputSchema':{'type':'object','properties':{'result':{'type':'string'}},
                        'required':['result'],'x-fastmcp-wrap-result':True}}])
    assert tools[0]['supported']
    output=tools[0]['outputSchema']
    checked(output,{'result':'process list'})
    with pytest.raises(ValueError):checked(output,{})
    with pytest.raises(ValueError):checked(output,{'result':42})


def test_pre_mcp_command_canonical_identity_remains_compatible():
    from nexus.contracts.extensions import canonical, digest_value
    original={'plugin_id':'builtin.shell','tool':'pwsh','input':{'command':'echo old'},'root_id':'workspace',
              'release_digest':RELEASES['builtin.shell'].digest(),'context_id':str(uuid4()),'invocation_id':str(uuid4()),
              'release':None,'capability_digests':{},'max_subcalls':8}
    restored=PluginNodeArguments.model_validate_json(canonical(original)).model_dump(mode='json')
    assert restored==original and digest_value(restored)==digest_value(original)


@pytest.mark.parametrize('stream',[False,True])
def test_real_streamable_http_discovery_and_call(stream):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from nexus.plugins.protocols import http
    tools=[{'name':'echo','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text']}}]
    methods=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            name=value['method'];methods.append(name)
            if name!='initialize':
                assert self.headers['Mcp-Session-Id']=='bounded-session'
                assert self.headers['MCP-Protocol-Version']=='2025-11-25'
            if name=='notifications/initialized':
                self.send_response(202);self.end_headers();return
            if name=='initialize':result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'http-fixture','version':'1'}}
            elif name=='tools/list':result={'tools':tools}
            else:result={'content':[{'type':'text','text':value['params']['arguments']['text']}],'isError':False}
            raw=json.dumps({'jsonrpc':'2.0','id':value['id'],'result':result},ensure_ascii=False).encode()
            if stream:raw=b'event: message\ndata: '+raw+b'\n\n'
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream' if stream else 'application/json')
            self.send_header('Mcp-Session-Id','bounded-session');self.send_header('Content-Length',str(len(raw)))
            self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        configuration={'url':f'http://127.0.0.1:{server.server_port}/mcp'}
        discovered=http(configuration,{},'nexus/discover')
        assert discovered['tools'][0]['name']=='echo'
        reply=http(configuration,{'name':'echo','arguments':{'text':'真实 HTTP 工具结果'}},'tools/call',discovered['catalog_digest'])
        assert reply['result']['content'][0]['text']=='真实 HTTP 工具结果'
        assert methods.count('tools/list')==2 and methods.count('tools/call')==1
    finally:
        server.shutdown();server.server_close();worker.join(timeout=2)


@pytest.mark.skipif(os.name!='nt',reason='Windows Job ownership')
def test_real_registration_discovery_call_catalog_change_and_reload(world,tmp_path):
    app,config,configuration=environment(world,tmp_path)
    async def run():
        cid=await seed(app,world)
        api=MCPHTTP(app.state.plugins,None)
        value=Register.model_validate_json(json.dumps({'request_id':str(uuid4()),'conversation_id':str(cid),'node_id':str(world.node_id),
            'server':'actual','expected_revision':0,'configuration':configuration}))
        submitted=await api.submit(world.owner,value,'mcp_register',{'server':'actual','expected_revision':0,'configuration':configuration})
        assert submitted['task'].status=='ready' and not submitted['approval_required']
        task=await execute(world,config,submitted['task'].task_id)
        assert task.status=='succeeded'
        await mcp.poll(app.state.plugins)
        async with world.db.transaction(write=False) as conn:
            active=await mcp.active(conn,world.owner_id,world.node_id,'actual')
            assert active['revision']==1 and json.loads(active['catalog_json'])[0]['name']=='add'
            digest=active['catalog_digest']
        retry=await api.submit(world.owner,value,'mcp_register',{'server':'actual','expected_revision':0,'configuration':configuration})
        assert retry['task'].task_id==task.task_id and retry['task'].status=='succeeded'
        async with world.db.transaction() as conn:
            bound=await mcp.prepare(conn,world.owner_id,world.node_id,cid,uuid4(),'mcp_call_tool',{'server':'actual','name':'add','arguments':{'a':4,'b':7},'catalog_digest':digest})
        runtime=PluginRuntime(config)
        try:
            args=PluginNodeArguments(plugin_id='builtin.mcp',tool='mcp_call_tool',input={'server':'actual','name':'add','arguments':{'a':4,'b':7},'catalog_digest':digest},
                context_id=cid,invocation_id=uuid4(),release_digest=RELEASES['builtin.mcp'].digest(),mcp_binding=bound)
            result=runtime.execute(args,time.monotonic()+10,threading.Event(),'unused')
            assert result.data['result']['structuredContent']=={'sum':11} and (tmp_path/'called').exists()
            (tmp_path/'called').unlink();(tmp_path/'changed').write_text('change')
            with pytest.raises(ValueError,match='catalog changed'):
                runtime.execute(args.model_copy(update={'invocation_id':uuid4()}),time.monotonic()+10,threading.Event(),'unused')
            assert not (tmp_path/'called').exists()
        finally:runtime.close()
        operation=Operation(request_id=uuid4(),conversation_id=cid,node_id=world.node_id,expected_revision=1)
        reloaded=await api.submit(world.owner,operation,'mcp_reload',{'server':'actual','expected_revision':1})
        await execute(world,config,reloaded['task'].task_id);await mcp.poll(app.state.plugins)
        async with world.db.transaction(write=False) as conn:
            active=await mcp.active(conn,world.owner_id,world.node_id,'actual')
            assert active['revision']==2 and active['catalog_digest']!=digest
    asyncio.run(run())


def test_normal_registration_waits_for_exact_task_approval_and_mode_change_blocks(world,tmp_path):
    app,config,configuration=environment(world,tmp_path)
    async def run():
        cid=await seed(app,world,'normal');api=MCPHTTP(app.state.plugins,None)
        value=Register.model_validate_json(json.dumps({'request_id':str(uuid4()),'conversation_id':str(cid),'node_id':str(world.node_id),
            'server':'normal','expected_revision':0,'configuration':configuration}))
        reply=await api.submit(world.owner,value,'mcp_register',{'server':'normal','expected_revision':0,'configuration':configuration})
        assert reply['approval_required'] and reply['task'].status=='awaiting_approval'
        assert not (tmp_path/'called').exists()
        await PermissionHTTP(app.state.chat,None).change(world.owner,ModeChange(request_id=uuid4(),expected_revision=1,mode='full'),cid)
        with pytest.raises(AccessDenied):await world.security.prepare_pending(reply['task'].task_id,world.node,world.scope)
        async with world.db.transaction(write=False) as conn:
            assert not await one(conn,"SELECT 1 FROM mcp_registrations WHERE state='active'")
    asyncio.run(run())


def test_http_owner_csrf_revision_and_cross_owner_boundaries(world,tmp_path):
    app,config,configuration=environment(world,tmp_path)
    asyncio.run(seed(app,world,'normal'))
    with TestClient(app,base_url=ORIGIN) as client:
        assert client.get('/api/v1/mcp',params={'node_id':str(world.node_id)}).status_code==401
        login=client.post('/api/v1/session',headers={'origin':ORIGIN},json={'token':world.owner_token.get_secret_value()})
        auth={'origin':ORIGIN,'x-csrf-token':login.json()['csrf_token']}
        cid=client.post('/api/v1/chat/conversations',headers=auth,json={'request_id':str(uuid4())}).json()['conversation']['conversation_id']
        value={'request_id':str(uuid4()),'conversation_id':cid,'node_id':str(world.node_id),'server':'api','expected_revision':0,'configuration':configuration}
        assert client.post('/api/v1/mcp',headers={'origin':ORIGIN},json=value).status_code==403
        reply=client.post('/api/v1/mcp',headers=auth,json=value)
        assert reply.status_code==200,reply.text
        assert reply.json()['approval_required']
        assert client.post('/api/v1/mcp',headers=auth,json=value).json()['task']['task_id']==reply.json()['task']['task_id']
        value['request_id']=str(uuid4())
        assert client.post('/api/v1/mcp',headers=auth,json=value).status_code==409
        value['conversation_id']=str(uuid4())
        assert client.post('/api/v1/mcp',headers=auth,json=value).status_code==404
        assert client.get(f'/api/v1/mcp/api/tools?node_id={world.node_id}').status_code==403
