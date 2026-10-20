"""Native SSE, independent Windows Node and web APIs through the real UI."""

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
            previous=next(m for m in reversed(value['messages']) if m.get('tool_calls'))
            if previous['tool_calls'][-1]['function']['name']=='web_search':
                args={'url':data['data']['sources'][0]['url']}
                yield event({'tool_calls':[{'index':0,'id':'ui-fetch','type':'function','function':{'name':'web_fetch','arguments':json.dumps(args)}}]},'tool_calls')
            elif len(previous['tool_calls'])==2:
                results=[json.loads(m['content']) for m in value['messages'] if m['role']=='tool']
                info=next(item['data'] for item in results if 'os_name' in item.get('data',{}))
                args={'root_id':'workspace','path':'computer-info.txt','content':'Computer information\n'+json.dumps(info,ensure_ascii=False)+'\n'}
                yield event({'tool_calls':[{'index':0,'id':'ui-write','type':'function','function':{'name':'write_file','arguments':json.dumps(args)}}]},'tool_calls')
            else:
                yield event({'content':'实际工作已完成，请查看工具证据。'},'stop')
        else:
            assert value.get('tools')
            write='write' in value['messages'][-1]['content']
            calls=[{'index':0,'id':'ui-directory','type':'function','function':{'name':'list_directory','arguments':json.dumps({'root_id':'workspace','limit':50})}},
                   {'index':1,'id':'ui-system','type':'function','function':{'name':'get_system_info','arguments':'{}'}}] if write else [{'index':0,'id':'ui-status','type':'function','function':{'name':'get_node_status','arguments':'{}'}}]
            if 'search public' in value['messages'][-1]['content']:
                calls=[{'index':0,'id':'ui-search','type':'function','function':{'name':'web_search','arguments':json.dumps({'query':'public information'})}}]
            yield event({'tool_calls':calls},'tool_calls')
        yield b'data: [DONE]\n\n'
    async def aclose(self):pass
async def simulated_web(request):
    assert str(request.url) in {'https://api.deepseek.com/v1/tools/search','https://api.deepseek.com/v1/tools/fetch'}
    return httpx2.Response(200,json={'results':[{'url':'https://example.com/','title':'Public source','snippet':'Synthetic source excerpt'}]} if request.url.path.endswith('search') else {'title':'Public page','content':'# External public evidence'})
app.state.chat.work.web.provider.transport=httpx2.MockTransport(simulated_web)
'''
WORK_SERVER=ACTION_SERVER[:ACTION_SERVER.index('class Stream')]+WORK_STREAM+'\n'+ACTION_SERVER[ACTION_SERVER.index('async def simulated'):]


@pytest.mark.skipif(os.name!="nt" or not os.environ.get("NEXUS_TEST_NODE"),reason="opt-in Windows Node / Edge browser evidence")
def test_chat_work_real_native_ui_and_node(world,tmp_path):
    config,node_config,origin=configure(world,tmp_path)
    config.write_text(config.read_text(encoding="utf-8").replace('chat_enabled=true','chat_enabled=true\nweb_enabled=true'),encoding="utf-8")
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
            info=(tmp_path/"workspace/computer-info.txt").read_text(encoding="utf-8")
            assert info.startswith("Computer information\n") and "Windows" in info
            assert (tmp_path/"workspace/example.txt").read_text(encoding="utf-8")=="not sent to model"
            (tmp_path/"browser-evidence.json").write_text(reply.stdout,encoding="utf-8")
        finally:
            stop(node);stop(core)
