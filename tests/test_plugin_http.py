"""Owner/CSRF and inherited-vs-overridden settings through the actual API."""

import asyncio
from uuid import uuid4
from fastapi.testclient import TestClient
from test_chat_skills import app_for
from test_node_link import ORIGIN
from test_plugin_lifecycle import candidate


def test_plugin_http_mode_inheritance_and_exact_version_actions(world,monkeypatch):
    app=app_for(world,monkeypatch)
    app.state.chat.plugins.config.enabled=True
    asyncio.run(app.state.chat.plugins.seed())
    with TestClient(app,base_url=ORIGIN) as client:
        assert client.get('/api/v1/plugins').status_code==401
        login=client.post('/api/v1/session',headers={'origin':ORIGIN},json={'token':world.owner_token.get_secret_value()})
        auth={'origin':ORIGIN,'x-csrf-token':login.json()['csrf_token']}
        conversation=client.post('/api/v1/chat/conversations',headers=auth,json={'request_id':str(uuid4())}).json()
        identifier=conversation['conversation']['conversation_id']
        route=f'/api/v1/chat/conversations/{identifier}/permission'
        assert client.put('/api/v1/permissions/default',json={'request_id':str(uuid4()),'expected_revision':1,'mode':'full'},headers={'origin':ORIGIN}).status_code==403
        result=client.put('/api/v1/permissions/default',headers=auth,json={'request_id':str(uuid4()),'expected_revision':1,'mode':'full'})
        assert result.status_code==200
        assert client.put('/api/v1/permissions/default',headers=auth,json={'request_id':str(uuid4()),'expected_revision':2,'mode':None}).status_code==400
        assert client.get(route).json()['effective_mode']=='full'
        assert client.put(route,headers=auth,json={'request_id':str(uuid4()),'expected_revision':1,'mode':'normal'}).json()['source']=='conversation'
        client.put('/api/v1/permissions/default',headers=auth,json={'request_id':str(uuid4()),'expected_revision':2,'mode':'normal'})
        assert client.get(route).json()['effective_mode']=='normal'
        assert client.put(route,headers=auth,json={'request_id':str(uuid4()),'expected_revision':2,'mode':None}).json()['source']=='global'
        version=client.post('/api/v1/plugins',headers=auth,json={'conversation_id':identifier,'request_id':str(uuid4()),'release':candidate().model_dump(mode='json')})
        assert version.status_code==200,version.text
        item=version.json()
        decision={'conversation_id':identifier,'digest':item['digest'],'action':'enable'}
        assert client.post(f"/api/v1/plugins/versions/{item['version_id']}/decision",headers=auth,json=decision).status_code==403
        snapshot=client.get(f'/api/v1/plugins/conversations/{identifier}').json()
        assert snapshot['todos']==[] and snapshot['schedules']==[]
