"""Bounded MCP resources and LSP navigation over configured transports only."""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def send(process,value,framed):
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
    process.stdin.write((f'Content-Length: {len(raw)}\r\n\r\n'.encode()+raw) if framed else raw+b'\n')
    process.stdin.flush()


def receive(process,identifier,framed):
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
            if length is None or not 0<length<=32768: raise ValueError('protocol message limit exceeded')
            raw=bytearray()
            while len(raw)<length:
                data=process.stdout.read(length-len(raw))
                if not data: raise ValueError('protocol closed mid-message')
                raw.extend(data)
        else:
            raw=process.stdout.readline(32769)
            if not raw or len(raw)>32768: raise ValueError('protocol message limit exceeded')
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


def stdio(server,params,method,cwd,framed=False,document=None):
    process=child(server,cwd)
    try:
        initialize=({'processId':None,'rootUri':Path(cwd).as_uri(),'capabilities':{'general':{'positionEncodings':['utf-16']}}} if framed else
                    {'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'nexus','version':'1.0.0'}})
        send(process,{'jsonrpc':'2.0','id':1,'method':'initialize','params':initialize},framed)
        result=receive(process,1,framed)
        if not isinstance(result,dict): raise ValueError('invalid initialization result')
        send(process,{'jsonrpc':'2.0','method':'initialized' if framed else 'notifications/initialized','params':{}},framed)
        if framed and document:
            if result.get('capabilities',{}).get('positionEncoding','utf-16')!='utf-16': raise ValueError('unsupported LSP position encoding')
            send(process,{'jsonrpc':'2.0','method':'textDocument/didOpen','params':{'textDocument':document}},True)
        send(process,{'jsonrpc':'2.0','id':2,'method':method,'params':params},framed)
        result=receive(process,2,framed)
        return {'result':result,'verification':'configured_server_response'}
    finally:
        if process.poll() is None: process.kill()
        process.wait(timeout=2)
        process.stdin.close();process.stdout.close()


def http(server,params,method):
    url=server['url']; parsed=urlsplit(url)
    if (parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','::1','localhost'})) or parsed.username or parsed.password:
        raise ValueError('MCP requires HTTPS or explicit loopback HTTP without URL credentials')
    headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream'}
    opener=build_opener(ProxyHandler({}),NoRedirect())
    def request(value):
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
                    if total>32768: raise ValueError('MCP event byte limit')
                    if line.startswith(b'data:'):
                        message=json.loads(line[5:].strip())
                        if message.get('id')==value.get('id'): return message
                raise ValueError('MCP response missing')
            raw=response.read(32769)
            if len(raw)>32768: raise ValueError('MCP response byte limit')
            return json.loads(raw)
    first=request({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'nexus','version':'1.0.0'}}})
    version=first.get('result',{}).get('protocolVersion') if isinstance(first,dict) else None
    if version not in {'2025-06-18','2025-03-26'}: raise ValueError('unsupported MCP protocol version')
    headers['MCP-Protocol-Version']=version
    request({'jsonrpc':'2.0','method':'notifications/initialized','params':{}})
    result=request({'jsonrpc':'2.0','id':2,'method':method,'params':params})
    if not isinstance(result,dict) or result.get('id')!=2 or 'error' in result: raise ValueError('MCP operation failed')
    return {'result':result.get('result',{}),'verification':'configured_server_response'}


def launch(runtime,payload,context,deadline,cancel):
    argv=([sys.executable,'--plugin-protocol'] if getattr(sys,'frozen',False) else [sys.executable,'-B','-m','nexus.plugins.protocols'])
    return runtime.json_process(argv,payload,context,deadline,cancel)


def mcp(runtime,value,name,context,deadline,cancel):
    server=runtime.policy.mcp.get(value['server'])
    if server is None or bool(server.url)==bool(server.process): raise ValueError('MCP server is not configured')
    method={'list_mcp_resources':'resources/list','list_mcp_resource_templates':'resources/templates/list','read_mcp_resource':'resources/read'}[name]
    params={key:value[key] for key in ('cursor','uri') if key in value}
    return launch(runtime,{'kind':'mcp','server':server.model_dump(mode='json'),'method':method,'params':params,'cwd':str(runtime.cwd(context))},context,deadline,cancel)


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
        elif value['server'].get('url'): result=http(value['server'],value['params'],value['method'])
        else: result=stdio(value['server']['process'],value['params'],value['method'],value['cwd'])
    except (OSError,ValueError,KeyError) as exc: result={'worker_error':str(exc)[:500]}
    sys.stdout.write(json.dumps(result,ensure_ascii=False,separators=(',',':')))
    return 0


if __name__=='__main__': raise SystemExit(main())
