"""Configured browser/app operations against owned, disposable real targets."""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import threading
from contextlib import contextmanager, closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from pydantic import TypeAdapter, ValidationError

from nexus.contracts.interaction import ObserveArguments, InteractArguments
from nexus.contracts.tools import ToolCall, validate_result
from nexus.node.tools.browser import browser_operation
from nexus.node.tools.apps import app_operation
from nexus.node.tools.interaction_config import BrowserTarget, AppTarget
from test_link_processes import wait_for


@contextmanager
def page_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body=b'<title>Controlled NEXUS page</title><input id="entry"><button id="apply" onclick="document.querySelector(\'output\').textContent=document.querySelector(\'input\').value">Apply</button><output>Before</output>'
            self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


@pytest.mark.skipif(os.name!="nt",reason="Windows browser targets")
@pytest.mark.parametrize("kind,relative",[("edge","Microsoft/Edge/Application/msedge.exe"),("chrome","Google/Chrome/Application/chrome.exe")])
def test_real_browser_observe_fill_click_and_stale_state(tmp_path,world,kind,relative):
    executable=next((Path(base)/relative for base in (os.environ.get("ProgramFiles(x86)",""),os.environ.get("ProgramFiles",""),os.environ.get("LOCALAPPDATA","")) if (Path(base)/relative).is_file()),None)
    if not executable:
        pytest.skip(f"{kind} not installed")
    from nexus.node.windows import WindowsJob
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1",0));port=reservation.getsockname()[1]
    endpoint=f"http://127.0.0.1:{port}"
    process=None
    with page_server() as origin,closing(WindowsJob("nexus-browser-test-"+uuid4().hex)) as job:
        try:
            process=subprocess.Popen([str(executable),"--headless=new",f"--remote-debugging-port={port}",f"--user-data-dir={tmp_path/'profile'}","--no-first-run","--no-default-browser-check",origin+"/"],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
            job.assign(process)
            with httpx2.Client(timeout=.5,trust_env=False) as client:
                wait_for(lambda:client.get(endpoint+"/json/list").status_code==200)
            target=BrowserTarget(kind=kind,endpoint=endpoint,allowed_origins=[origin],actions={"fill":{"kind":"fill","selector":"#entry","description":"填写测试输入"},"apply":{"kind":"click","selector":"#apply","description":"更新测试输出"}})
            observe=ObserveArguments(root_id="workspace",target_id="test-page",template_digest=target.digest())
            first=wait_for(lambda:browser_operation(observe,target))
            assert "Before" in first.observation.state_json
            fill=InteractArguments(**observe.model_dump(),action_id="fill",expected_state_sha256=first.observation.state_sha256,value="NEXUS tested")
            second=browser_operation(fill,target)
            assert "NEXUS tested" in second.observation.state_json and second.observation.state_sha256!=first.observation.state_sha256
            call=TypeAdapter(ToolCall).validate_python({"tool":"interaction.act","tool_version":1,"arguments":fill})
            validate_result(call,second,32768)
            with pytest.raises(PermissionError):
                browser_operation(fill,target)
            done=browser_operation(InteractArguments(**observe.model_dump(),action_id="apply",expected_state_sha256=second.observation.state_sha256),target)
            assert "NEXUS tested" in json.loads(done.observation.state_json)["text"]
            if kind=="edge":
                linked_browser(world,tmp_path,target)
        finally:
            job.close()
            if process:
                process.wait(timeout=5)


@pytest.mark.parametrize("endpoint",["https://127.0.0.1:9222","http://localhost:9222","http://10.0.0.1:9222","http://user@127.0.0.1:9222","http://127.0.0.1:9222/path"])
def test_browser_endpoint_never_accepts_remote_or_credentialed_targets(endpoint):
    with pytest.raises(ValidationError):
        BrowserTarget(kind="edge",endpoint=endpoint,allowed_origins=["https://example.com"])


@pytest.mark.skipif(os.name!="nt",reason="Windows UI Automation")
def test_real_configured_application_and_state_binding(tmp_path):
    csc=Path(os.environ["SystemRoot"])/"Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not csc.is_file():
        pytest.skip("test compiler unavailable")
    source=tmp_path/"OwnedNexusApp.cs";executable=tmp_path/"OwnedNexusApp.exe"
    source.write_text('''using System;using System.Windows;using System.Windows.Controls;using System.Windows.Automation;
class OwnedNexusApp { [STAThread] static void Main() {var app=new Application();var f=new Window();f.Title="NEXUS controlled test";f.Width=350;f.Height=120;var t=new TextBox();AutomationProperties.SetAutomationId(t,"entry");t.Text="before";f.Content=t;app.Run(f);} }''',encoding="utf-8")
    compiled=subprocess.run([str(csc),"/nologo","/target:winexe","/r:"+str(csc.parent/"System.Xaml.dll"),*["/r:"+str(csc.parent/"WPF"/name) for name in ("PresentationFramework.dll","PresentationCore.dll","WindowsBase.dll")],"/out:"+str(executable),str(source)],capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    assert compiled.returncode==0,compiled.stdout.decode(errors="replace")
    process=subprocess.Popen([str(executable)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        import time
        time.sleep(.5)
        target=AppTarget(kind="windows_app",executable=executable,executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),actions={"fill":{"kind":"set_value","automation_id":"entry","description":"更新测试输入框"}})
        args=ObserveArguments(root_id="workspace",target_id="test-app",template_digest=target.digest())
        first=app_operation(args,target)
        assert "before" in first.observation.state_json
        from nexus.node.config import NodeConfig
        from nexus.node.worker import WorkerRequest, RESULT
        from nexus.contracts.messages import CommandExecute
        from nexus.node.process import ManagedWorker
        from nexus.core.storage.database import utc_now
        from datetime import timedelta
        config=NodeConfig(node_id=uuid4(),data_dir=tmp_path,roots={"workspace":tmp_path},allowed_tools=frozenset({"interaction.observe@1"}),interaction_targets={"test-app":target})
        command=TypeAdapter(CommandExecute).validate_python({"session_id":uuid4(),"task_id":uuid4(),"attempt_id":uuid4(),"grant_id":uuid4(),"command_digest":"a"*64,"expires_at":utc_now()+timedelta(seconds=10),"execution_timeout_seconds":10,"tool":"interaction.observe","tool_version":1,"arguments":args})
        job_name="nexus-owned-app-worker-"+uuid4().hex
        worker=WorkerRequest(command=command,config=config,job_name=job_name)
        evidence=RESULT.validate_json(ManagedWorker().run(worker.model_dump_json().encode(),job_name,10,threading.Event()))
        assert evidence.outcome=="succeeded" and "before" in evidence.result.observation.state_json
        assert process.poll() is None  # Closing the bridge job never closes the configured app.
        fill=InteractArguments(**args.model_dump(),action_id="fill",expected_state_sha256=first.observation.state_sha256,value="after")
        second=app_operation(fill,target)
        assert "after" in second.observation.state_json
        with pytest.raises(ValueError):
            app_operation(fill,target)
        with pytest.raises(PermissionError):
            app_operation(args.model_copy(update={"template_digest":"a"*64}),target)
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def linked_browser(world,tmp_path,target):
    """Actual Core -> WSS-style v1 Link -> independent Node -> CDP, fake SSE only."""
    import sys
    from test_chat_action_processes import configure, ACTION_SERVER
    from test_link_processes import spawn, stop
    config,node_config,origin=configure(world,tmp_path)
    tools=["interaction.observe@1","interaction.act@1"]
    async def grants():
        for tool in tools:
            for identity in (world.owner_id,world.node_id):
                await world.security.set_grant(identity,tool[:-2],root_id="workspace")
    asyncio.run(grants())
    for path in (config,node_config):
        path.write_text(path.read_text(encoding="utf-8").replace('["system.info@1","filesystem.list@1"]',json.dumps(tools)),encoding="utf-8")
    with config.open("a",encoding="utf-8") as out:
        out.write(f'\n[nodes."{world.node_id}".interaction_digests]\ntest-page="{target.digest()}"\n')
    with node_config.open("a",encoding="utf-8") as out:
        out.write(f'\n[interaction_targets.test-page]\nkind="edge"\nendpoint="{target.endpoint}"\nallowed_origins={json.dumps(target.allowed_origins)}\n')
        for identifier,action in target.actions.items():
            out.write(f'\n[interaction_targets.test-page.actions.{identifier}]\nkind="{action.kind}"\ndescription={json.dumps(action.description,ensure_ascii=False)}\nselector={json.dumps(action.selector)}\n')
    stream=r'''
class Stream(httpx2.AsyncByteStream):
    def __init__(self,value):self.value=value
    async def __aiter__(self):
        value=self.value
        if value['messages'][-1]['role']=='tool':
            result=json.loads(value['messages'][-1]['content'])
            assert result['status']=='succeeded'
            data=result['data']
            if data['action_id'] is None:
                name='interact_target';args={'root_id':'workspace','target_id':'test-page','action_id':'fill','value':'From linked Node','expected_state_sha256':data['observation']['state_sha256']}
            else:
                assert 'From linked Node' in data['observation']['state_json']
                name=None
        else:
            name='observe_target';args={'root_id':'workspace','target_id':'test-page'}
        delta={'tool_calls':[{'index':0,'id':'linked-'+name,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]} if name else {'content':'已核对固定目标实际结果。'}
        body={'model':'deepseek-flash','choices':[{'index':0,'delta':delta,'finish_reason':'tool_calls' if name else 'stop'}],'usage':{'prompt_tokens':100,'completion_tokens':20}}
        yield ('data: '+json.dumps(body)+'\n\n').encode();yield b'data: [DONE]\n\n'
    async def aclose(self):pass
'''
    server=ACTION_SERVER[:ACTION_SERVER.index('class Stream')]+stream+ACTION_SERVER[ACTION_SERVER.index('async def simulated'):]
    environment={key:value for key,value in os.environ.items() if not key.startswith("NEXUS_")}
    environment["DEEPSEEK_API_KEY"]="synthetic-interaction-no-external-call"
    core=node=None
    with (tmp_path/"linked.log").open("w+b") as log,httpx2.Client(base_url=origin,trust_env=False,timeout=3) as client:
        try:
            core=subprocess.Popen([sys.executable,"-B","-c",server,str(config)],env=environment,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
            wait_for(lambda:client.get("/").status_code==200)
            session=client.post("/api/v1/session",json={"token":world.owner_token.get_secret_value()},headers={"origin":origin});auth={"origin":origin,"x-csrf-token":session.json()["csrf_token"]}
            node=spawn("nexus.node",node_config,log,token=world.node_token.get_secret_value())
            online=wait_for(lambda:(item if (item:=client.get("/api/v1/nodes").json()["nodes"][0])["online"] else None))
            assert online["interaction_digests"]["test-page"]==target.digest()
            def post(path,value):
                response=client.post(path,json=value,headers=auth);assert response.status_code==200,response.text
                return response.json()
            conv=post("/api/v1/chat/conversations",{"request_id":str(uuid4())})["conversation"]
            base="/api/v1/chat/conversations/"+conv["conversation_id"]
            grant=post(base+"/work-grant",{"request_id":str(uuid4()),"node_id":str(world.node_id),"consent":True,"profile":"read"})["grant"]
            directory=client.get("/api/v1/chat/work-catalog",params={"node_id":str(world.node_id)}).json()
            turn=post(base+"/turns",{"request_id":str(uuid4()),"expected_revision":conv["revision"],"text":"操作固定测试页面","allow_model":True,"allow_tools":True,"node_id":str(world.node_id),"work_grant_id":grant["grant_id"],"work_digest":directory["work_digest"],"allow_work_results":True})["turn"]
            path="/api/v1/chat/turns/"+turn["turn_id"]
            permission=wait_for(lambda:(item if (item:=client.get(path).json()["turn"])["work_status"]=="permission" else None))
            assert permission["work_steps"][0]["result"]["verification"]=="node_report_schema_checked"
            assert "From linked Node" not in browser_operation(ObserveArguments(root_id="workspace",target_id="test-page",template_digest=target.digest()),target).observation.state_json
            post(path+"/permission",{"request_id":str(uuid4()),"step_id":permission["work_steps"][1]["step_id"],"expected_revision":permission["revision"],"text":"同意"})
            done=wait_for(lambda:(item if (item:=client.get(path).json()["turn"])["status"]=="completed" and not item["work_status"] else None))
            assert done["work_steps"][1]["result"]["data"]["verification"]=="configured_action_returned"
            assert len(client.get(path+"/journal").json()["exchanges"])==3
        finally:
            stop(node);stop(core)
