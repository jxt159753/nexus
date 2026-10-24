"""Bounded MCP discovery/tools/resources and configured LSP navigation."""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler
from nexus.plugins.mcp_contracts import catalog, check_call

MCP_VERSIONS={'2025-11-25','2025-06-18','2025-03-26'}


def mcp_operation(request, initialized, method, params, expected_catalog=None):
    if initialized.get('protocolVersion') not in MCP_VERSIONS:
        raise ValueError('unsupported MCP protocol version')
    if method not in {'nexus/discover','tools/call'}:
        return {'result':request(method,params),'verification':'configured_server_response'}
    tools=[];cursor=None
    if 'tools' in initialized.get('capabilities',{}):
        for _ in range(8):
            page=request('tools/list',{'cursor':cursor} if cursor else {})
            if not isinstance(page,dict) or not isinstance(page.get('tools'),list): raise ValueError('invalid MCP tools response')
            tools.extend(page['tools'])
            if len(tools)>64: raise ValueError('MCP tool count limit exceeded')
            next_cursor=page.get('nextCursor')
            if next_cursor is None: break
            if not isinstance(next_cursor,str) or not next_cursor or len(next_cursor)>1000 or next_cursor==cursor: raise ValueError('invalid MCP pagination')
            cursor=next_cursor
        else: raise ValueError('MCP discovery page limit exceeded')
    tools,digest=catalog(tools)
    if method=='nexus/discover':
        info=initialized.get('serverInfo',{})
        return {'tools':tools,'catalog_digest':digest,'server_info':{key:str(info.get(key,''))[:100] for key in ('name','version')},
                'protocol_version':initialized['protocolVersion'],'verification':'mcp_tools_discovered'}
    if not expected_catalog or expected_catalog!=digest: raise ValueError('MCP catalog changed; reload is required')
    definition=check_call(tools,params['name'],params.get('arguments',{}))
    result=request('tools/call',params)
    if not isinstance(result,dict): raise ValueError('invalid MCP tool result')
    if 'outputSchema' in definition and not result.get('isError'):
        from nexus.plugins.schema import checked
        checked(definition['outputSchema'],result.get('structuredContent'))
    return {'result':result,'catalog_digest':digest,'status':'failed' if result.get('isError') else 'succeeded','verification':'mcp_tool_response_checked'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def send(process,value,framed):
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
    process.stdin.write((f'Content-Length: {len(raw)}\r\n\r\n'.encode()+raw) if framed else raw+b'\n')
    process.stdin.flush()


def receive(process,identifier,framed,max_bytes=32768):
    for _ in range(100):
        if framed:
            length=None; headers=0
            while True:
                line=process.stdout.readline(4097)
                headers+=len(line)
                if not line or headers>8192: raise ValueError('invalid protocol header')
                if line==b'\r\n': break
                key,_,item=line.decode('ascii').partition(':')
                if key.lower()=='content-length': length=int(item.strip())
            if length is None or not 0<length<=max_bytes: raise ValueError('protocol message limit exceeded')
            raw=bytearray()
            while len(raw)<length:
                data=process.stdout.read(length-len(raw))
                if not data: raise ValueError('protocol closed mid-message')
                raw.extend(data)
        else:
            raw=process.stdout.readline(max_bytes+1)
            if not raw or len(raw)>max_bytes: raise ValueError('protocol message limit exceeded')
        value=json.loads(raw)
        if value.get('id')==identifier:
            if 'error' in value: raise ValueError('server returned protocol error: '+str(value['error'])[:300])
            return value.get('result',{})
        if 'id' in value and 'method' in value:
            send(process,{'jsonrpc':'2.0','id':value['id'],'error':{'code':-32601,'message':'Client method not supported'}},framed)
    raise ValueError('protocol event limit exceeded')


def child(server,cwd):
    executable=Path(server['executable'])
    if not executable.is_absolute() or not executable.is_file(): raise ValueError('configured executable unavailable')
    environment={key:value for key,value in os.environ.items() if not key.upper().startswith(('NEXUS_','DEEPSEEK_','OPENAI_','ANTHROPIC_'))}
    return subprocess.Popen([str(executable),*server.get('argv',[])],cwd=cwd,env=environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                            shell=False,close_fds=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),bufsize=0)


def stdio(server,params,method,cwd,framed=False,document=None,expected_catalog=None):
    process=child(server,cwd)
    try:
        initialize=({'processId':None,'rootUri':Path(cwd).as_uri(),'capabilities':{'general':{'positionEncodings':['utf-16']}}} if framed else
                    {'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'nexus','version':'1.0.0'}})
        send(process,{'jsonrpc':'2.0','id':1,'method':'initialize','params':initialize},framed)
        result=receive(process,1,framed)
        if not isinstance(result,dict): raise ValueError('invalid initialization result')
        if not framed and result.get('protocolVersion') not in MCP_VERSIONS: raise ValueError('unsupported MCP protocol version')
        send(process,{'jsonrpc':'2.0','method':'initialized' if framed else 'notifications/initialized','params':{}},framed)
        if framed and document:
            if result.get('capabilities',{}).get('positionEncoding','utf-16')!='utf-16': raise ValueError('unsupported LSP position encoding')
            send(process,{'jsonrpc':'2.0','method':'textDocument/didOpen','params':{'textDocument':document}},True)
        counter=1
        def request(name,value):
            nonlocal counter
            counter+=1
            send(process,{'jsonrpc':'2.0','id':counter,'method':name,'params':value},framed)
            from nexus.plugins.mcp_images import MAX_MESSAGE_BYTES
            return receive(process,counter,framed,MAX_MESSAGE_BYTES if not framed and name=='tools/call' else 32768)
        if framed: return {'result':request(method,params),'verification':'configured_server_response'}
        return mcp_operation(request,result,method,params,expected_catalog)
    finally:
        if process.poll() is None: process.kill()
        process.wait(timeout=2)
        process.stdin.close();process.stdout.close()


def http(server,params,method,expected_catalog=None):
    url=server['url']; parsed=urlsplit(url)
    if (parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','::1','localhost'})) or parsed.username or parsed.password:
        raise ValueError('MCP requires HTTPS or explicit loopback HTTP without URL credentials')
    headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream'}
    opener=build_opener(ProxyHandler({}),NoRedirect())
    def request(value):
        from nexus.plugins.mcp_images import MAX_MESSAGE_BYTES
        max_bytes=MAX_MESSAGE_BYTES if value.get('method')=='tools/call' else 32768
        with opener.open(Request(url,data=json.dumps(value).encode(),headers=headers,method='POST'),timeout=2) as response:
            session=response.headers.get('Mcp-Session-Id')
            if session:
                if not 0<len(session)<=256 or any(not 33<=ord(c)<=126 for c in session): raise ValueError('invalid MCP session identifier')
                headers['Mcp-Session-Id']=session
            if response.status==202: return None
            if response.headers.get_content_type()=='text/event-stream':
                total=0
                for line in response:
                    total+=len(line)
                    if total>max_bytes: raise ValueError('MCP event byte limit')
                    if line.startswith(b'data:'):
                        message=json.loads(line[5:].strip())
                        if message.get('id')==value.get('id'): return message
                raise ValueError('MCP response missing')
            raw=response.read(max_bytes+1)
            if len(raw)>max_bytes: raise ValueError('MCP response byte limit')
            return json.loads(raw)
    first=request({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'nexus','version':'1.0.0'}}})
    version=first.get('result',{}).get('protocolVersion') if isinstance(first,dict) else None
    if version not in MCP_VERSIONS: raise ValueError('unsupported MCP protocol version')
    headers['MCP-Protocol-Version']=version
    request({'jsonrpc':'2.0','method':'notifications/initialized','params':{}})
    counter=1
    def rpc(name,value):
        nonlocal counter
        counter+=1
        result=request({'jsonrpc':'2.0','id':counter,'method':name,'params':value})
        if not isinstance(result,dict) or result.get('id')!=counter or 'error' in result: raise ValueError('MCP operation failed')
        return result.get('result',{})
    return mcp_operation(rpc,first['result'],method,params,expected_catalog)


def launch(runtime,payload,context,deadline,cancel):
    argv=([sys.executable,'--plugin-protocol'] if getattr(sys,'frozen',False) else [sys.executable,'-B','-m','nexus.plugins.protocols'])
    if payload['kind']!='mcp':
        return runtime.json_process(argv,payload,context,deadline,cancel)
    from tempfile import TemporaryDirectory
    from nexus.plugins.mcp_images import upload
    with TemporaryDirectory(prefix='mcp-images-',dir=runtime.config.data_dir) as directory:
        data=runtime.json_process(argv,{**payload,'image_directory':directory},context,deadline,cancel)
        return upload(runtime,data,directory,deadline,cancel)


def mcp(runtime,value,name,context,deadline,cancel):
    server=runtime.policy.mcp.get(value['server'])
    binding=getattr(runtime.invocation,'mcp_binding',None)
    if name in {'mcp_register','mcp_reload','mcp_discover','mcp_call_tool'} and binding is None:
        raise PermissionError('MCP management and tool calls require a Core registry binding')
    if binding is not None:
        if not runtime.policy.allow_managed_mcp or binding.server!=value['server']: raise PermissionError('managed MCP not allowed by Node policy')
        server=binding.configuration
    if server is None or bool(server.url)==bool(server.process): raise ValueError('MCP server is not configured')
    method={'list_mcp_resources':'resources/list','list_mcp_resource_templates':'resources/templates/list','read_mcp_resource':'resources/read',
            'mcp_register':'nexus/discover','mcp_reload':'nexus/discover','mcp_discover':'nexus/discover','mcp_call_tool':'tools/call'}[name]
    params={key:value[key] for key in ('cursor','uri','name','arguments') if key in value}
    expected=value.get('catalog_digest')
    if name=='mcp_call_tool' and binding and binding.catalog_digest!=expected: raise PermissionError('MCP catalog binding mismatch')
    return launch(runtime,{'kind':'mcp','server':server.model_dump(mode='json'),'method':method,'params':params,'expected_catalog':expected,'cwd':str(runtime.cwd(context))},context,deadline,cancel)


def lsp(runtime,value,context,deadline,cancel):
    server=runtime.policy.lsp.get(value['server'])
    if server is None: raise ValueError('LSP server is not configured')
    from nexus.contracts.basic import relative_path
    relative_path(value['path'])
    from nexus.node.tools.paths import guarded_path,read_handle
    with guarded_path(runtime.root,value['path']) as path,read_handle(path) as stream:
        content=stream.read(24577)
        if len(content)>24576: raise ValueError('LSP document exceeds byte limit')
        uri=path.as_uri()
    params={'textDocument':{'uri':uri},'position':{'line':value['line']-1,'character':value['character']-1}}
    if value['operation']=='references': params['context']={'includeDeclaration':True}
    document={'uri':uri,'languageId':{'py':'python','rs':'rust','ts':'typescript','js':'javascript'}.get(path.suffix[1:],path.suffix[1:]),'version':1,'text':content.decode('utf-8')}
    return launch(runtime,{'kind':'lsp','server':server.model_dump(mode='json'),'method':'textDocument/'+value['operation'],'params':params,'document':document,'cwd':str(runtime.cwd(context))},context,deadline,cancel)


def main():
    try:
        raw=sys.stdin.buffer.readline(65537)
        if not raw or len(raw)>65536: return 2
        value=json.loads(raw)
        if value['kind']=='lsp': result=stdio(value['server'],value['params'],value['method'],value['cwd'],True,value.get('document'))
        elif value['server'].get('url'): result=http(value['server'],value['params'],value['method'],value.get('expected_catalog'))
        else: result=stdio(value['server']['process'],value['params'],value['method'],value['cwd'],expected_catalog=value.get('expected_catalog'))
        if value['kind']=='mcp':
            from nexus.plugins.mcp_images import extract
            result=extract(result,value['image_directory'])
    except (OSError,ValueError,KeyError) as exc: result={'worker_error':str(exc)[:500]}
    sys.stdout.write(json.dumps(result,ensure_ascii=False,separators=(',',':')))
    return 0


if __name__=='__main__': raise SystemExit(main())
