"""Independent Core + Windows Node, real installation and MCP persistence."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from uuid import uuid4

import httpx2
import pytest

from nexus.contracts.extensions import SkillGeneration
from nexus.plugins.catalog import RELEASES
from nexus.node.windows import WindowsJob
from test_link_processes import spawn,stop,wait_for
from test_managed_mcp import environment,seed,SERVER
from test_native_skills import generation,approved


@pytest.mark.skipif(os.name!='nt',reason='real Windows Node processes')
@pytest.mark.parametrize('frozen',[False,True])
def test_real_core_node_skill_installs_and_registers_mcp_then_restart(world,tmp_path,frozen):
    executable=os.environ.get('NEXUS_TEST_FROZEN_NODE') if frozen else None
    if frozen and not executable:pytest.skip('fixed Node executable was not supplied')
    app,config,_=environment(world,tmp_path)
    generated=tmp_path/'installed_mcp.py'
    model=generation().model_dump(mode='json')
    model['steps'][0]['arguments']['command']="$source=@'\n"+SERVER+"\n'@\n[IO.File]::WriteAllText('"+str(generated)+"',$source,[Text.UTF8Encoding]::new($false))"
    model['steps'].append({'id':'register','tool':'plugin.call','tool_version':1,'plugin_id':'builtin.mcp','plugin_tool':'mcp_register','plugin_digest':RELEASES['builtin.mcp'].digest(),
        'arguments':{'server':'installed','expected_revision':0,'configuration':{'process':{'executable':sys.executable,'argv':['-B',str(generated)]}}}})
    model['manifest']['output_schema']={'type':'object','properties':{key:{'type':'object','additionalProperties':True} for key in ('install','register')},'required':['install','register'],'additionalProperties':False}
    model['tests'][0]['expected_steps']=['install','register']
    async def setup():
        cid=await seed(app,world)
        version=await approved(app,world,SkillGeneration.model_validate_json(json.dumps(model)))
        return cid,version
    cid,version=asyncio.run(setup())
    assert not generated.exists()
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1',0));port=reserved.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    node_dir=tmp_path/'node-data';node_dir.mkdir()
    config=config.model_copy(update={'data_dir':node_dir,'core_url':f'ws://127.0.0.1:{port}/ws/v1/node','local_http':True})
    core=tmp_path/'core.toml';node=tmp_path/'node.toml'
    core.write_text(f'''data_dir={json.dumps(str(world.db.path.parent))}
owner_id="{world.owner_id}"
host="127.0.0.1"
port={port}
local_http=true
origins=["{origin}"]
[extensions]
enabled=true
fixture_generation=true
[plugins]
enabled=true
[nodes."{world.node_id}"]
tools=["plugin.call@1"]
roots=["workspace"]
plugin_digest="{config.plugin_digest()}"
''',encoding='utf-8')
    node.write_text(f'''node_id="{world.node_id}"
data_dir={json.dumps(str(node_dir))}
core_url="{config.core_url}"
local_http=true
allowed_tools=["plugin.call@1"]
[roots]
workspace={json.dumps(str(tmp_path))}
[plugins]
enabled=true
allow_managed_mcp=true
pwsh={json.dumps(str(config.plugins.pwsh))}
''',encoding='utf-8')
    core_process=node_process=None
    node_job=WindowsJob('nexus-test-frozen-'+uuid4().hex) if frozen else None
    with (tmp_path/'process.log').open('w+b') as log,httpx2.Client(base_url=origin,timeout=3,trust_env=False) as browser:
        try:
            core_process=spawn('nexus.core',core,log)
            wait_for(lambda:browser.get('/api/v1/nodes').status_code==401)
            login=browser.post('/api/v1/session',headers={'origin':origin},json={'token':world.owner_token.get_secret_value()})
            auth={'origin':origin,'x-csrf-token':login.json()['csrf_token']}
            if frozen:
                environment_values={key:value for key,value in os.environ.items() if not key.startswith('NEXUS_')}
                environment_values['NEXUS_NODE_TOKEN']=world.node_token.get_secret_value()
                node_process=subprocess.Popen([executable,'--config',str(node)],stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                    env=environment_values,creationflags=subprocess.CREATE_NO_WINDOW)
                node_job.assign(node_process)
            else:node_process=spawn('nexus.node',node,log,token=world.node_token.get_secret_value())
            wait_for(lambda:browser.get('/api/v1/nodes').json()['nodes'][0]['online'])
            response=browser.post('/api/v1/extensions/runs',headers=auth,json={'request_id':str(uuid4()),'version_id':version,'node_id':str(world.node_id),'conversation_id':str(cid),'input':{}})
            assert response.status_code==200,response.text
            run_id=response.json()['run']['run_id']
            def finished():
                value=browser.get('/api/v1/extensions/runs/'+run_id).json()['run']
                return value if value['status'] not in {'running','cancelling'} else None
            final=wait_for(finished,timeout=35)
            assert final['status']=='succeeded',final
            assert generated.is_file()
            tools=browser.get(f'/api/v1/mcp/installed/tools?node_id={world.node_id}')
            assert tools.status_code==200,tools.text
            assert tools.json()['tools'][0]['name']=='add'
            digest=tools.json()['catalog_digest']
            stop(core_process);core_process=spawn('nexus.core',core,log)
            wait_for(lambda:browser.get('/api/v1/nodes').status_code==200)
            wait_for(lambda:browser.get('/api/v1/nodes').json()['nodes'][0]['online'])
            assert browser.get(f'/api/v1/mcp/installed/tools?node_id={world.node_id}').json()['catalog_digest']==digest
            assert browser.get('/api/v1/extensions/runs/'+run_id).json()['run']['status']=='succeeded'
            failed=browser.post('/api/v1/mcp',headers=auth,json={'request_id':str(uuid4()),'node_id':str(world.node_id),'conversation_id':str(cid),
                'server':'installed','expected_revision':1,'configuration':{'process':{'executable':sys.executable,'argv':['-B',str(tmp_path/'does_not_exist.py')]}}})
            assert failed.status_code==200,failed.text
            bad_task=failed.json()['task']['task_id']
            wait_for(lambda:browser.get('/api/v1/tasks/'+bad_task).json()['task']['status']=='failed')
            def failed_reload():
                page=browser.get(f'/api/v1/mcp?node_id={world.node_id}').json()['servers']
                return any(item['server']=='installed' and item['revision']==2 and item['state']=='failed' for item in page)
            wait_for(failed_reload)
            assert browser.get(f'/api/v1/mcp/installed/tools?node_id={world.node_id}').json()['catalog_digest']==digest
            removed=browser.request('DELETE','/api/v1/mcp/installed',headers=auth,json={'request_id':str(uuid4()),'node_id':str(world.node_id),'conversation_id':str(cid),'expected_revision':2})
            assert removed.status_code==200,removed.text
            assert browser.get(f'/api/v1/mcp/installed/tools?node_id={world.node_id}').status_code==403
            assert browser.get('/api/v1/models/usage').json()['calls']==0
        finally:
            if node_job:node_job.close()
            stop(node_process);stop(core_process)
        log.seek(0);output=log.read().decode('utf-8',errors='replace')
        assert world.owner_token.get_secret_value() not in output and world.node_token.get_secret_value() not in output
