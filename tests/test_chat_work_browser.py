"""Native SSE + independent Windows Node + natural approval through real UI."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx2
import pytest

from test_chat_action_processes import ACTION_SERVER, configure
from test_link_processes import spawn, stop, wait_for

WORK_STREAM = r'''
class Stream(httpx2.AsyncByteStream):
    def __init__(self,value):self.value=value
    async def __aiter__(self):
        value=self.value
        def event(delta,finish=None):
            body={'model':'deepseek-flash','choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
            if finish:body['usage']={'prompt_tokens':100,'completion_tokens':20}
            return ('data: '+json.dumps(body,ensure_ascii=False)+'\n\n').encode()
        if value['messages'][-1]['role']=='tool':
            data=json.loads(value['messages'][-1]['content'])
            assert data['status']=='succeeded'
            yield event({'content':'实际工作已完成，请查看工具证据。'},'stop')
        else:
            assert value.get('tools')
            write='write' in value['messages'][-1]['content']
            name='write_file' if write else 'get_node_status'
            args={'root_id':'workspace','path':'example.txt','content':'approved via natural chat'} if write else {}
            yield event({'tool_calls':[{'index':0,'id':'real-ui-native-call','type':'function','function':{'name':name,'arguments':json.dumps(args)}}]},'tool_calls')
        yield b'data: [DONE]\n\n'
    async def aclose(self):pass
'''
WORK_SERVER=ACTION_SERVER[:ACTION_SERVER.index('class Stream')]+WORK_STREAM+'\n'+ACTION_SERVER[ACTION_SERVER.index('async def simulated'):]


@pytest.mark.skipif(os.name!="nt" or not os.environ.get("NEXUS_TEST_NODE"),reason="opt-in Windows Node / Edge browser evidence")
def test_chat_work_real_native_ui_and_node(world,tmp_path):
    config,node_config,origin=configure(world,tmp_path)
    tools=["system.info@1","filesystem.list@1","filesystem.read@1","filesystem.write_preview@1","filesystem.write@1"]
    async def grants():
        for key in tools:
            for identity in (world.owner_id,world.node_id):
                await world.security.set_grant(identity,key[:-2],root_id="" if key.startswith("system") else "workspace")
    asyncio.run(grants())
    for path in (config,node_config):
        path.write_text(path.read_text(encoding="utf-8").replace('["system.info@1","filesystem.list@1"]',json.dumps(tools)),encoding="utf-8")
    env={key:value for key,value in os.environ.items() if not key.startswith("NEXUS_")}
    env["DEEPSEEK_API_KEY"]="synthetic-work-browser-no-network"
    core=node=None
    with (tmp_path/"process.log").open("w+b") as log,httpx2.Client(base_url=origin,timeout=3,trust_env=False) as client:
        try:
            core=subprocess.Popen([sys.executable,"-B","-c",WORK_SERVER,str(config)],env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
            wait_for(lambda:client.get("/").status_code==200)
            node=spawn("nexus.node",node_config,log,token=world.node_token.get_secret_value())
            reply=subprocess.run([os.environ["NEXUS_TEST_NODE"],str(Path(__file__).with_name("chat_work_browser.cjs"))],input=json.dumps({"origin":origin,"token":world.owner_token.get_secret_value(),"desktop":str(tmp_path/"work-desktop.png"),"mobile":str(tmp_path/"work-mobile.png")}),text=True,encoding="utf-8",capture_output=True,timeout=120,creationflags=subprocess.CREATE_NO_WINDOW)
            assert reply.returncode == 0,reply.stderr
            assert (tmp_path/"workspace/example.txt").read_text(encoding="utf-8") == "approved via natural chat"
            (tmp_path/"browser-evidence.json").write_text(reply.stdout,encoding="utf-8")
        finally:
            stop(node);stop(core)
