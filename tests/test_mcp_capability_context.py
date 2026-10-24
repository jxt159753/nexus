"""A phone-origin chat must see the computer's actual MCP registrations."""

import asyncio
import json
from uuid import uuid4

import pytest

from nexus.core.cognitive import node_routing, work_catalog
from nexus.core.models.chat import request_body
from nexus.core.models.config import ModelConfig
from nexus.core.plugins import mcp
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import utc_now
from test_managed_mcp import environment, seed
from test_node_routing import phone


async def registration(conn, owner, node, server, revision=1, state='active'):
    await conn.execute(
        'INSERT INTO mcp_registrations '
        '(registration_id,owner_id,node_id,server,revision,configuration_json,configuration_digest,state,created_at) '
        'VALUES(?,?,?,?,?,?,?,?,?)',
        (str(uuid4()), str(owner), str(node), server, revision,
         json.dumps({'process': {'executable': 'C:/private/python.exe', 'argv': ['private-argument']}}),
         'a' * 64, state, utc_now().isoformat()),
    )


def test_phone_catalog_exposes_only_each_devices_registered_mcp(world, tmp_path):
    app, _, _ = environment(world, tmp_path)

    async def run():
        await seed(app, world)
        mobile = await phone(app, world)
        async with world.db.transaction() as conn:
            await registration(conn, world.owner_id, world.node_id, 'windows-desktop', revision=2)
            await registration(conn, world.owner_id, mobile.identity_id, 'phone-service')
            # A different owner must not leak even if the target node matches.
            await registration(conn, mobile.identity_id, world.node_id, 'other-owner-service')
            before = await conn.execute_fetchall('SELECT * FROM grants ORDER BY grant_id')
            directory = await node_routing.directory(app.state.chat, conn, world.owner)
            after = await conn.execute_fetchall('SELECT * FROM grants ORDER BY grant_id')
            assert [tuple(r) for r in before] == [tuple(r) for r in after]
            assert not await conn.execute_fetchall('SELECT * FROM tasks')
            local = await work_catalog.catalog(app.state.chat, conn, world.owner, world.node_id)
        entries = directory['catalog']
        for entry in entries:
            if 'route_node_id' in entry:
                entry['client_device'] = entry['route_node_id'] == str(mobile.identity_id)
        desktop = next(e for e in entries if e.get('route_node_id') == str(world.node_id)
                       and e.get('route_intent') == 'plugin_inspect')
        mobile_inspect = next(e for e in entries if e.get('route_node_id') == str(mobile.identity_id)
                              and e.get('route_intent') == 'plugin_inspect')
        assert 'windows-desktop' in desktop['description']
        assert 'windows-desktop' not in mobile_inspect['description']
        assert 'phone-service' in mobile_inspect['description']
        assert 'phone-service' not in desktop['description']
        assert 'windows-desktop' in next(e for e in local['catalog'] if e['name'] == 'plugin_inspect')['description']
        app.state.chat.config.chat_input_bytes = 65536
        body = request_body(app.state.chat.config, [{'role': 'user', 'content': '你能否调用Windows mcp'}],
                            allow_tools=True, work_catalog=entries)
        request = json.loads(body)
        assert 'windows-desktop' in body.decode()
        assert all(secret not in body.decode() for secret in ('private/python.exe', 'private-argument', 'other-owner-service'))
        assert 'Entry device label' in request['messages'][0]['content']
        assert not any(e.get('route_node_id') == str(mobile.identity_id)
                       and e.get('route_intent') == 'plugin_execute' for e in entries)

    asyncio.run(run())


def test_phone_question_can_inspect_computer_without_execution_or_extra_approval(world, tmp_path, monkeypatch):
    from nexus.contracts.chat import ChatSubmission, ConversationCreate
    from test_node_routing import RoutedProvider

    monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-no-network')
    app, _, _ = environment(world, tmp_path, models=ModelConfig(enabled=True, chat_enabled=True))

    async def run():
        await seed(app, world, mode='normal')
        mobile = await phone(app, world)
        chat = app.state.chat
        chat.provider = RoutedProvider([(mobile.identity_id, 'plugin_inspect', {'plugin_id': 'builtin.mcp'}),
                                        (world.node_id, 'plugin_inspect', {'plugin_id': 'builtin.mcp'})])
        async with world.db.transaction() as conn:
            await registration(conn, world.owner_id, world.node_id, 'windows-desktop', revision=2)
            directory = await node_routing.directory(chat, conn, world.owner)
        conv = await chat.create(world.owner, ConversationCreate(request_id=uuid4()))
        turn = await chat.submit(world.owner, conv['conversation_id'], ChatSubmission(
            request_id=uuid4(), expected_revision=conv['revision'], text='你能否调用Windows mcp',
            allow_model=True, allow_tools=True, client_node_id=mobile.identity_id,
            allow_work_results=True, work_digest=directory['work_digest']))
        for _ in range(7):
            await chat.process_one()
        result = await chat.get(world.owner, turn['turn_id'])
        assert result['status'] == 'completed'
        assert [s['status'] for s in result['work_steps']] == ['succeeded', 'succeeded']
        mobile_data, desktop_data = [s['result']['data'] for s in result['work_steps']]
        assert mobile_data['registrations']['servers'] == []
        assert desktop_data['registrations']['servers'][0]['server'] == 'windows-desktop'
        # Registered does not imply connected, permitted, or successfully executed.
        assert desktop_data['execution']['status'] == 'node_offline'
        assert desktop_data['execution']['node_id'] == str(world.node_id)
        async with world.db.transaction(write=False) as conn:
            assert not await conn.execute_fetchall('SELECT * FROM tasks')
            assert not await conn.execute_fetchall('SELECT * FROM approvals')
            assert sum(r[0] for r in await conn.execute_fetchall('SELECT used_operations FROM chat_work_grants')) == 2
            assert (await node_routing.directory(chat, conn, world.owner))['work_digest'] == directory['work_digest']

    asyncio.run(run())


def test_mcp_summary_is_bounded_and_does_not_resurrect_removed_registration(world, tmp_path):
    app, _, _ = environment(world, tmp_path)

    async def run():
        await seed(app, world)
        async with world.db.transaction() as conn:
            await registration(conn, world.owner_id, world.node_id, 'a-removed', state='inactive')
            await registration(conn, world.owner_id, world.node_id, 'a-removed', revision=2, state='removed')
            for index in range(21):
                await registration(conn, world.owner_id, world.node_id, f'server-{index:02}')
            listing = await mcp.model_list(conn, world.owner_id, world.node_id)
            assert len(listing['servers']) == 20 and listing['truncated']
            assert listing['servers'][0]['state'] == 'removed'
            with pytest.raises(AccessDenied, match='MCP_SERVER_UNAVAILABLE'):
                await mcp.active(conn, world.owner_id, world.node_id, 'a-removed')
            directory = await work_catalog.catalog(app.state.chat, conn, world.owner, world.node_id)
            description = next(e['description'] for e in directory['catalog'] if e['name'] == 'plugin_inspect')
            assert '"truncated":true' in description and 'server-20' not in description

    asyncio.run(run())


@pytest.mark.parametrize('new_state', ['pending', 'failed'])
def test_registry_summary_keeps_active_revision_during_failed_or_pending_update(world, tmp_path, new_state):
    app, _, _ = environment(world, tmp_path)

    async def run():
        await seed(app, world)
        async with world.db.transaction() as conn:
            await registration(conn, world.owner_id, world.node_id, 'desktop', state='active')
            await registration(conn, world.owner_id, world.node_id, 'desktop', revision=2, state=new_state)
            summary = await mcp.model_list(conn, world.owner_id, world.node_id)
            detail = await mcp.model_list(conn, world.owner_id, world.node_id, 'desktop')
            assert summary['servers'][0]['state'] == detail['state'] == 'active'
            assert summary['servers'][0]['revision'] == detail['revision'] == 1
            assert summary['servers'][0]['latest_revision'] == detail['latest_revision'] == 2

    asyncio.run(run())
