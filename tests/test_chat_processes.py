"""Independent Core process, simulated real SSE, crash/restart without replays."""

import json
import os
import socket
import subprocess
import sys
from uuid import uuid4

import httpx2

from test_link_processes import stop, wait_for


CHAT_SERVER = r'''
import asyncio,json,sys,uvicorn,httpx2
from pathlib import Path
from nexus.core.app import create_app
from nexus.core.config import CoreConfig
from nexus.service_config import load_config
config=load_config(Path(sys.argv[1]),CoreConfig,{})
app=create_app(config)
def event(text=None,finish=None):
    value={'model':'deepseek-flash','choices':[{'index':0,'delta':{'content':text} if text else {},'finish_reason':finish}]}
    if finish: value['usage']={'prompt_tokens':100,'completion_tokens':20}
    return ('data: '+json.dumps(value,ensure_ascii=False)+chr(10)+chr(10)).encode()
class Stream(httpx2.AsyncByteStream):
    def __init__(self,messages): self.messages=messages
    async def __aiter__(self):
        text=self.messages[-1]['content']
        assert all(m['role'] in {'system','user','assistant'} for m in self.messages)
        if 'BLOCK' in text:
            yield event('这是已经收到的部分回复。')
            await asyncio.Event().wait()
        else:
            answer='展开第二个方案：制作一个解谜游戏。' if len(self.messages)>2 else chr(10).join(['1. 平台跳跃游戏','2. 解谜游戏','','```python','print("hello")','```','<img src=x onerror="window.chatInjected=true">'])
            for offset in range(0,len(answer),5):
                yield event(answer[offset:offset+5])
                await asyncio.sleep(.06)
            yield event(finish='stop')
            yield b'data: [DONE]'+bytes([10,10])
    async def aclose(self): pass
async def provider(request):
    assert str(request.url)=='https://api.deepseek.com/chat/completions'
    value=json.loads(request.content)
    assert value['stream'] and 'tools' not in value
    return httpx2.Response(200,headers={'content-type':'text/event-stream'},stream=Stream(value['messages']))
app.state.chat.provider.transport=httpx2.MockTransport(provider)
uvicorn.run(app,host=config.host,port=config.port,access_log=False,log_level='warning',ws='websockets-sansio')
'''


def test_chat_independent_process_stream_stop_crash_and_history(world, tmp_path):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1",0))
        port=reserved.getsockname()[1]
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
''',encoding="utf-8")
    env={k:v for k,v in os.environ.items() if not k.startswith("NEXUS_")}
    env["DEEPSEEK_API_KEY"]="synthetic-chat-test-no-api"
    core=None
    with (tmp_path/"process.log").open("w+b") as log, httpx2.Client(base_url=origin,trust_env=False,timeout=3) as browser:
        def start():
            return subprocess.Popen([sys.executable,"-B","-c",CHAT_SERVER,str(config)],env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            core=start()
            wait_for(lambda:browser.get("/").status_code==200)
            login=browser.post("/api/v1/session",json={"token":world.owner_token.get_secret_value()},headers={"origin":origin})
            auth={"origin":origin,"x-csrf-token":login.json()["csrf_token"]}
            conv=browser.post("/api/v1/chat/conversations",json={"request_id":str(uuid4())},headers=auth).json()["conversation"]
            path=f"/api/v1/chat/conversations/{conv['conversation_id']}/turns"
            def send(text):
                revision=browser.get(path).json()["conversation"]["revision"]
                return browser.post(path,json={"request_id":str(uuid4()),"expected_revision":revision,"text":text,"allow_model":True},headers=auth).json()["turn"]
            def turn(identifier):
                return browser.get(f"/api/v1/chat/turns/{identifier}").json()["turn"]
            first=send("我想做一个游戏，给我两个方向")
            complete=wait_for(lambda:(value if (value:=turn(first["turn_id"]))["status"]=="completed" else None))
            assert "解谜" in complete["response_text"]
            second=send("展开第二个方向")
            followup=wait_for(lambda:(value if (value:=turn(second["turn_id"]))["status"]=="completed" else None))
            assert followup["context_count"]==1 and followup["response_text"].startswith("展开第二个")
            blocking=send("BLOCK，测试停止")
            wait_for(lambda:turn(blocking["turn_id"])["response_text"])
            browser.post(f"/api/v1/chat/turns/{blocking['turn_id']}/stop",json={},headers=auth)
            stopped=wait_for(lambda:(value if (value:=turn(blocking["turn_id"]))["status"]=="stopped" else None))
            assert stopped["cost_status"]=="unknown" and stopped["response_text"]
            crash=send("BLOCK，测试崩溃恢复")
            wait_for(lambda:turn(crash["turn_id"])["response_text"])
            first_pid=core.pid
            stop(core)
            core=start()
            wait_for(lambda:browser.get("/api/v1/session").status_code==200)
            recovered=turn(crash["turn_id"])
            assert core.pid!=first_pid and recovered["status"]=="interrupted"
            assert recovered["cost_status"]=="unknown" and recovered["response_text"]
            assert turn(first["turn_id"])["response_text"]==complete["response_text"]
            assert browser.get("/api/v1/models/usage").json()["calls"]==4
            assert browser.get("/api/v1/tasks").json()["tasks"]==[]
        finally:
            stop(core)
        log.seek(0)
        output=log.read().decode("utf-8",errors="replace")
        assert world.owner_token.get_secret_value() not in output and env["DEEPSEEK_API_KEY"] not in output
