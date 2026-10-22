"""Real managed MCP stdio and a pinned Pyright server, with owned processes."""

import json
import os
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4

import pytest
from nexus.node.config import NodeConfig
from nexus.plugins.config import PluginNodeConfig,ProcessServer,MCPServer
from nexus.plugins.node_runtime import PluginRuntime

pytestmark=pytest.mark.skipif(os.name!='nt',reason='Windows managed protocol workers')


def test_real_mcp_resource_server_and_no_server_invention(tmp_path):
    source=tmp_path/'mcp_server.py'
    source.write_text('''import sys,json
for line in sys.stdin:
    v=json.loads(line)
    if 'id' not in v: continue
    method=v['method']
    if method=='initialize': result={'protocolVersion':'2025-06-18','capabilities':{'resources':{}},'serverInfo':{'name':'test','version':'1'}}
    elif method=='resources/list': result={'resources':[{'uri':'test://actual','name':'actual'}]}
    elif method=='resources/templates/list': result={'resourceTemplates':[{'uriTemplate':'test://{id}','name':'template'}]}
    else: result={'contents':[{'uri':v['params']['uri'],'mimeType':'text/plain','text':'actual resource'}]}
    print(json.dumps({'jsonrpc':'2.0','id':v['id'],'result':result}),flush=True)
''',encoding='utf-8')
    runtime=PluginRuntime(NodeConfig(node_id=uuid4(),data_dir=tmp_path,roots={'workspace':tmp_path},plugins=PluginNodeConfig(enabled=True,mcp={'test':MCPServer(process=ProcessServer(executable=Path(sys.executable),argv=['-B',str(source)]))})))
    try:
        for name,key in [('list_mcp_resources','resources'),('list_mcp_resource_templates','resourceTemplates'),('read_mcp_resource','contents')]:
            value={'server':'test',**({'uri':'test://actual'} if name=='read_mcp_resource' else {})}
            result=runtime.call('builtin.mcp',name,value,str(uuid4()),time.monotonic()+10,threading.Event())
            assert result['result'][key]
        with pytest.raises(ValueError): runtime.call('builtin.mcp','list_mcp_resources',{'server':'invented'},str(uuid4()),time.monotonic()+10,threading.Event())
    finally: runtime.close()


def test_real_pyright_definition_and_hover(tmp_path):
    node=Path('C:/Program Files/nodejs/node.exe')
    server=Path(__file__).resolve().parents[1]/'var/plugins-runtime/node_modules/pyright/langserver.index.js'
    if not node.is_file() or not server.is_file(): pytest.skip('pinned local Pyright runtime not installed')
    (tmp_path/'example.py').write_text('def increment(value: int) -> int:\n    return value + 1\n\nresult = increment(5)\n',encoding='utf-8')
    runtime=PluginRuntime(NodeConfig(node_id=uuid4(),data_dir=tmp_path,roots={'workspace':tmp_path},plugins=PluginNodeConfig(enabled=True,lsp={'python':ProcessServer(executable=node,argv=[str(server),'--stdio'])})))
    try:
        value={'server':'python','operation':'definition','path':'example.py','line':4,'character':11}
        result=runtime.call('builtin.workspace','lsp',value,str(uuid4()),time.monotonic()+10,threading.Event())
        assert result['result'] and result['result'][0]['range']['start']['line']==0
        value['operation']='hover'
        result=runtime.call('builtin.workspace','lsp',value,str(uuid4()),time.monotonic()+10,threading.Event())
        assert 'increment' in json.dumps(result['result'])
    finally: runtime.close()
