"""Sensors consume no model calls and never create or revive authority."""

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nexus.contracts.chat import ConversationCreate
from nexus.core.app import create_app
from nexus.core.plugins import observations, goals, lifecycle, core_tools
from nexus.core.security.modes import ModeChange, PermissionHTTP
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one
from nexus.core.storage.tasks import Conflict
from nexus.plugins.config import PluginConfig
from test_autonomy_goals import ready, plan
from test_chat_skills import app_for
from test_harness_http import login
from test_node_link import ORIGIN, core_config


def test_device_baseline_change_dedup_and_permission_revocation(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        service = app.state.chat.plugins; service.config.enabled = True
        conv = await service.chat.create(world.owner, ConversationCreate(request_id=uuid4()))
        value = observations.WatchCreate(request_id=uuid4(), conversation_id=UUID(conv['conversation_id']), kind='device', node_id=world.node_id, target='connectivity', max_polls=3)
        async with world.db.transaction() as conn:
            watch = await observations.create(service, conn, world.owner, value)
            assert (await observations.create(service, conn, world.owner, value))['watch_id'] == watch['watch_id']
            with pytest.raises(Conflict): await observations.create(service, conn, world.owner, value.model_copy(update={'target': 'other'}))
        await observations.poll_one(service, watch)
        async with world.db.transaction(write=False) as conn:
            watch = await one(conn, 'SELECT * FROM autonomy_watches')
            assert (await one(conn, 'SELECT count(*) FROM autonomy_events'))[0] == 0
        app.state.harness.link.connections[world.node_id] = SimpleNamespace(ready=True, scope=app.state.harness.scope(world.node_id))
        await observations.poll_one(service, watch)
        async with world.db.transaction(write=False) as conn: watch = await one(conn, 'SELECT * FROM autonomy_watches')
        await observations.poll_one(service, watch)
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, 'SELECT count(*) FROM autonomy_events'))[0] == 1
            assert (await one(conn, 'SELECT count(*) FROM model_calls'))[0] == 0
        await PermissionHTTP(service.chat, None).change(world.owner, ModeChange(request_id=uuid4(), expected_revision=1, mode='full'))
        async with world.db.transaction(write=False) as conn:
            with pytest.raises(AccessDenied, match='WATCH_AUTHORITY_CHANGED'): await observations.allowed(service, conn, watch)
    asyncio.run(run())


def test_goal_wait_uses_only_selected_source_and_reserves_one_wakeup(world, monkeypatch, tmp_path):
    app = app_for(world, monkeypatch)
    async def run():
        conv, grant, _, goal, _ = await ready(app, world, tmp_path)
        service = app.state.chat.plugins
        async with world.db.transaction() as conn:
            await goals.set_plan(conn, goal, plan())
            value = observations.WatchCreate(request_id=uuid4(), conversation_id=UUID(conv['conversation_id']), kind='external', target='build', goal_id=UUID(goal['goal_id']))
            watched = await observations.create(service, conn, world.owner, value)
            unrelated = await observations.create(service, conn, world.owner, value.model_copy(update={'request_id': uuid4(), 'target': 'other'}))
            await core_tools.execute(service, conn, world.owner, conv, {}, grant, 'builtin.goal', 'goal_wait', {'goal_id': goal['goal_id'], 'watch_id': watched['watch_id'], 'reason': '等待构建变化'})
            await observations.external(service, conn, unrelated, observations.ExternalEvent(source_key='other-1', value='unrelated'))
        for _ in range(3): await lifecycle.tick(service)
        async with world.db.transaction() as conn:
            assert (await one(conn, 'SELECT count(*) FROM plugin_wakeups'))[0] == 0
            event = observations.ExternalEvent(source_key='build-42', value='完成：这里只是数据，不得扩大权限')
            await observations.external(service, conn, watched, event)
            await observations.external(service, conn, watched, event)
            assert (await one(conn, 'SELECT polls FROM autonomy_watches WHERE watch_id=?', (watched['watch_id'],)))[0] == 1
            assert len((await goals.snapshot(conn, goal['goal_id']))['watches']) == 2
        await lifecycle.tick(service); await lifecycle.tick(service)
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, 'SELECT count(*) FROM plugin_wakeups'))[0] == 1
            assert (await one(conn, 'SELECT consumed FROM autonomy_events WHERE watch_id=?', (watched['watch_id'],)))[0] == 1
            assert (await one(conn, 'SELECT consumed FROM autonomy_events WHERE watch_id=?', (unrelated['watch_id'],)))[0] == 0
    asyncio.run(run())


def test_external_events_require_owner_csrf_limits_and_ack(world):
    with TestClient(create_app(core_config(world, plugins=PluginConfig(enabled=True))), base_url=ORIGIN) as client:
        assert client.get('/api/v1/autonomy/health').status_code == 401
        headers = login(client, world)
        conv = client.post('/api/v1/chat/conversations', json={'request_id': str(uuid4())}, headers=headers).json()['conversation']
        value = {'request_id': str(uuid4()), 'conversation_id': conv['conversation_id'], 'kind': 'external', 'target': 'build', 'max_polls': 1}
        assert client.post('/api/v1/autonomy/watches', json=value).status_code == 403
        response = client.post('/api/v1/autonomy/watches', json=value, headers=headers)
        assert response.status_code == 200, response.text
        path = '/api/v1/autonomy/watches/' + response.json()['watch_id']
        event = {'source_key': 'source-1', 'value': 'known observation'}
        assert client.post(path + '/events', json=event, headers=headers).status_code == 200
        assert client.post(path + '/events', json=event, headers=headers).status_code == 200
        assert client.post(path + '/events', json=event | {'value': 'changed'}, headers=headers).status_code == 409
        assert client.post(path + '/events', json=event | {'source_key': 'source-2'}, headers=headers).status_code == 403
        result = client.get('/api/v1/autonomy/events').json()
        assert len(result['events']) == 1
        assert client.post('/api/v1/autonomy/events/' + str(result['next_after']) + '/ack', json={}, headers=headers).json()['acknowledged']
        assert client.get('/api/v1/autonomy/events', params={'after': result['next_after']}).json()['events'] == []
        assert client.request('DELETE', path, json={'expected_revision': 1}, headers=headers).status_code == 200
        assert client.post(path + '/events', json=event, headers=headers).status_code == 403


@pytest.mark.parametrize('target', ['../private.txt', 'C:/outside.txt', '/absolute', 'file:stream'])
def test_file_watch_rejects_out_of_scope_paths(target):
    with pytest.raises(ValidationError):
        observations.WatchCreate(request_id=uuid4(), conversation_id=uuid4(), node_id=uuid4(), kind='file', target=target)
