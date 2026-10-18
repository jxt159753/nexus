"""Independent Core/Windows Node chat actions with simulated provider SSE."""

import json
import os
import socket
import subprocess
import sys
from uuid import uuid4

import httpx2

from test_link_processes import spawn, stop, wait_for
from test_node_link import grant


ACTION_SERVER=r'''
import asyncio,json,sys,uvicorn,httpx2
from pathlib import Path
from nexus.core.app import create_app
from nexus.core.config import CoreConfig
from nexus.service_config import load_config
config=load_config(Path(sys.argv[1]),CoreConfig,{})
app=create_app(config)
class Stream(httpx2.AsyncByteStream):
    def __init__(self,value):self.value=value
    async def __aiter__(self):
        value=self.value
        def event(delta,finish=None,usage=None):
            body={'model':'deepseek-flash','choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
            if usage is not None:body['usage']=usage
            return ('data: '+json.dumps(body,ensure_ascii=False)+'\n\n').encode()
        title='{"title":"工作区检查"}\n' if 'Start your content' in value['messages'][0]['content'] else ''
        if value.get('tools'):
            assert len(value['tools'])==2 and value['tool_choice']=='auto'
            directory='directory' in value['messages'][-1]['content'] or '目录' in value['messages'][-1]['content']
            name='list_directory' if directory else 'get_system_info'
            arguments=json.dumps({'root_id':'workspace','limit':2} if directory else {})
            yield event({'content':title+'已准备只读动作，请确认。'})
            yield event({'tool_calls':[{'index':0,'id':'call-synthetic','type':'function','function':{'name':name,'arguments':arguments[:1]}}]})
            await asyncio.sleep(.1)
            yield event({'tool_calls':[{'index':0,'function':{'arguments':arguments[1:]}}]})
            yield event({},'tool_calls',{'prompt_tokens':100,'completion_tokens':20})
        else:
            yield event({'content':title+'这是普通聊天，不执行工具。'})
            yield event({},'stop',{'prompt_tokens':100,'completion_tokens':20})
        yield b'data: [DONE]\n\n'
    async def aclose(self):pass
async def simulated(request):
    assert str(request.url)=='https://api.deepseek.com/chat/completions'
    return httpx2.Response(200,headers={'content-type':'text/event-stream'},stream=Stream(json.loads(request.content)))
app.state.chat.provider.transport=httpx2.MockTransport(simulated)
uvicorn.run(app,host=config.host,port=config.port,access_log=False,log_level='warning',ws='websockets-sansio')
'''


def configure(world,tmp_path):
    grant(world);grant(world,"filesystem.list")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1",0));port=reserved.getsockname()[1]
    origin=f"http://127.0.0.1:{port}"
    config=tmp_path/"core.toml"
    config.write_text(f'''data_dir={json.dumps(str(world.db.path.parent))}
owner_id="{world.owner_id}"
host="127.0.0.1"
port={port}
local_http=true
origins=["{origin}"]
[models]
enabled=true
chat_enabled=true
[nodes."{world.node_id}"]
tools=["system.info@1","filesystem.list@1"]
roots=["workspace"]
''',encoding="utf-8")
    root=tmp_path/"workspace";root.mkdir()
    (root/"example.txt").write_text("not sent to model",encoding="utf-8")
    node_dir=tmp_path/"node";node_dir.mkdir()
    node_config=tmp_path/"node.toml"
    node_config.write_text(f'''node_id="{world.node_id}"
data_dir={json.dumps(str(node_dir))}
core_url="ws://127.0.0.1:{port}/ws/v1/node"
local_http=true
allowed_tools=["system.info@1","filesystem.list@1"]
[roots]
workspace={json.dumps(str(root))}
''',encoding="utf-8")
    return config,node_config,origin


def test_chat_actions_real_tools_confirmation_approval_and_restart(world,tmp_path):
    config,node_config,origin=configure(world,tmp_path)
    env={k:v for k,v in os.environ.items() if not k.startswith("NEXUS_")}
    env["DEEPSEEK_API_KEY"]="synthetic-no-external-api"
    core=node=None
    with (tmp_path/"process.log").open("w+b") as log,httpx2.Client(base_url=origin,timeout=3,trust_env=False) as client:
        def start():
            return subprocess.Popen([sys.executable,"-B","-c",ACTION_SERVER,str(config)],env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            core=start();wait_for(lambda:client.get("/").status_code==200)
            session=client.post("/api/v1/session",json={"token":world.owner_token.get_secret_value()},headers={"origin":origin})
            auth={"origin":origin,"x-csrf-token":session.json()["csrf_token"]}
            node=spawn("nexus.node",node_config,log,token=world.node_token.get_secret_value())
            wait_for(lambda:client.get("/api/v1/nodes").json()["nodes"][0]["online"])
            conv=client.post("/api/v1/chat/conversations",json={"request_id":str(uuid4())},headers=auth).json()["conversation"]
            path="/api/v1/chat/conversations/"+conv["conversation_id"]+"/turns"
            results=[]
            for text in ("please check this computer's system","please list workspace directory"):
                history=client.get(path).json()
                turn=client.post(path,json={"request_id":str(uuid4()),"expected_revision":history["conversation"]["revision"],"text":text,"allow_model":True,"allow_tools":True,"node_id":str(world.node_id)},headers=auth).json()["turn"]
                turn_path="/api/v1/chat/turns/"+turn["turn_id"]
                proposed=wait_for(lambda:(item if (item:=client.get(turn_path).json()["turn"])["action_status"]=="proposed" else None))
                assert proposed["status"]=="completed" and proposed["task_id"] is None
                assert len(client.get("/api/v1/tasks").json()["tasks"])==len(results)
                value={"decision":"confirm","proposal_digest":proposed["proposal_digest"]}
                task=client.post(turn_path+"/action",json=value,headers=auth).json()["task"]
                assert client.post(turn_path+"/action",json=value,headers=auth).json()["task"]["task_id"]==task["task_id"]
                if "directory" in text:
                    assert task["status"]=="awaiting_approval"
                    detail=client.get("/api/v1/tasks/"+task["task_id"]).json()
                    assert detail["attempts"]==[]
                    assert client.post("/api/v1/approvals/"+detail["approvals"][0]["approval_id"],json={"decision":"approve"},headers=auth).status_code==200
                detail=wait_for(lambda:(item if (item:=client.get("/api/v1/tasks/"+task["task_id"]).json())["task"]["status"]=="succeeded" else None))
                assert len(detail["attempts"])==1 and detail["task"]["verification"]=="node_report_schema_checked"
                results.append(detail)
            assert results[0]["task"]["result"]["os_name"]=="Windows"
            assert results[1]["task"]["result"]["entries"]==[{"name":"example.txt","kind":"file"}]
            history=client.get(path).json()
            assert history["conversation"]["title"]=="工作区检查" and "title" not in history["turns"][0]["response_text"]
            usage=client.get("/api/v1/models/usage").json();assert usage["calls"]==2
            stop(core);core=start();wait_for(lambda:client.get("/api/v1/session").status_code==200)
            assert client.get(path).json()==history and client.get("/api/v1/models/usage").json()==usage
            for detail in results:assert client.get("/api/v1/tasks/"+detail["task"]["task_id"]).json()==detail
        finally:stop(node);stop(core)
        log.seek(0);output=log.read().decode(errors="replace")
        assert world.owner_token.get_secret_value() not in output and world.node_token.get_secret_value() not in output and env["DEEPSEEK_API_KEY"] not in output
