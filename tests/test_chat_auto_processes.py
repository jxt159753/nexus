"""Recovery during automatic tool feedback with real owned Core/Node processes."""

import json
import os
import subprocess
import sys
from uuid import uuid4

import httpx2
import pytest

from test_chat_action_processes import ACTION_SERVER, configure
from test_link_processes import spawn, stop, wait_for


@pytest.mark.parametrize("stage", ["waiting_for_node", "final_generating"])
def test_automatic_result_recovery_never_reexecutes_tool_or_model(world, tmp_path, stage):
    config,node_config,origin=configure(world,tmp_path)
    env={k:v for k,v in os.environ.items() if not k.startswith('NEXUS_')};env['DEEPSEEK_API_KEY']='synthetic-only'
    core=node=None
    with (tmp_path/'process.log').open('w+b') as log,httpx2.Client(base_url=origin,timeout=3,trust_env=False) as client:
        def start():
            return subprocess.Popen([sys.executable,'-B','-c',ACTION_SERVER,str(config)],env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            core=start();wait_for(lambda:client.get('/').status_code==200)
            session=client.post('/api/v1/session',json={'token':world.owner_token.get_secret_value()},headers={'origin':origin})
            auth={'origin':origin,'x-csrf-token':session.json()['csrf_token']}
            conv=client.post('/api/v1/chat/conversations',json={'request_id':str(uuid4())},headers=auth).json()['conversation']
            path='/api/v1/chat/conversations/'+conv['conversation_id']+'/turns'
            turn=client.post(path,json={'request_id':str(uuid4()),'expected_revision':conv['revision'],'text':('BLOCKFINAL ' if stage=='final_generating' else '')+'查看电脑系统','allow_model':True,'allow_tools':True,'node_id':str(world.node_id)},headers=auth).json()['turn']
            turn_path='/api/v1/chat/turns/'+turn['turn_id']
            turn=wait_for(lambda:(item if (item:=client.get(turn_path).json()['turn'])['action_status']=='proposed' else None))
            task=client.post(turn_path+'/action',json={'decision':'confirm','proposal_digest':turn['proposal_digest'],'allow_result_model':True},headers=auth).json()['task']
            if stage=='waiting_for_node':
                assert client.get(path).json()['active_turn_id']==turn['turn_id']
                assert client.get('/api/v1/models/usage').json()['calls']==1
                stop(core);core=start();wait_for(lambda:client.get('/api/v1/session').status_code==200)
            node=spawn('nexus.node',node_config,log,token=world.node_token.get_secret_value())
            if stage=='final_generating':
                wait_for(lambda:(item if (item:=client.get(turn_path).json()['turn'])['phase']=='summary' and item['status']=='generating' and item['response_text'] else None))
                core.kill();core.wait(timeout=10)
                core=start();wait_for(lambda:client.get('/api/v1/session').status_code==200)
            final=wait_for(lambda:(item if (item:=client.get(turn_path).json()['turn'])['phase']=='summary' and item['status'] in {'completed','interrupted'} else None))
            assert final['status']==('completed' if stage=='waiting_for_node' else 'interrupted')
            assert final['cost_status']==('calculated' if stage=='waiting_for_node' else 'unknown')
            detail=client.get('/api/v1/tasks/'+task['task_id']).json()
            assert detail['task']['status']=='succeeded' and len(detail['attempts'])==1
            usage=client.get('/api/v1/models/usage').json();assert usage['calls']==2
            history=client.get(path).json()
            stop(core);core=start();wait_for(lambda:client.get('/api/v1/session').status_code==200)
            assert client.get(path).json()==history and client.get('/api/v1/models/usage').json()==usage
        finally:stop(node);stop(core)
