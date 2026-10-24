"""Defaults, explicit overrides and stale-authority rejection across restarts."""

import asyncio
from uuid import UUID, uuid4

import pytest

from nexus.contracts.chat import ConversationCreate
from nexus.core.security.modes import ModeChange, PermissionHTTP, effective
from nexus.core.security.types import AccessDenied
from nexus.core.security.work import valid_grant
from nexus.core.storage.tasks import Conflict
from nexus.core.storage.database import one, utc_now
from test_chat_skills import app_for
from test_chat_work import setup


def test_default_inheritance_override_and_compare_exchange(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        first = await chat.create(world.owner, ConversationCreate(request_id=uuid4()))
        second = await chat.create(world.owner, ConversationCreate(request_id=uuid4()))
        api = PermissionHTTP(chat, None)
        async def mode(identifier):
            async with world.db.transaction(write=False) as conn:
                return await effective(conn, identifier)
        assert (await mode(first['conversation_id']))['effective_mode'] == 'normal'
        full = ModeChange(request_id=uuid4(), expected_revision=1, mode='full')
        assert (await api.change(world.owner, full))['mode'] == 'full'
        assert await api.change(world.owner, full) == {'mode': 'full', 'revision': 2}
        assert (await mode(first['conversation_id']))['effective_mode'] == 'full'
        override = await api.change(world.owner, ModeChange(request_id=uuid4(), expected_revision=1, mode='normal'), UUID(first['conversation_id']))
        assert override['effective_mode'] == 'normal' and override['source'] == 'conversation'
        await api.change(world.owner, ModeChange(request_id=uuid4(), expected_revision=2, mode='normal'))
        await api.change(world.owner, ModeChange(request_id=uuid4(), expected_revision=3, mode='full'))
        assert (await mode(first['conversation_id']))['effective_mode'] == 'normal'
        assert (await mode(second['conversation_id']))['effective_mode'] == 'full'
        reset = await api.change(world.owner, ModeChange(request_id=uuid4(), expected_revision=2, mode=None), UUID(first['conversation_id']))
        assert reset['source'] == 'global' and reset['effective_mode'] == 'full'
        with pytest.raises(Conflict):
            await api.change(world.owner, ModeChange(request_id=uuid4(), expected_revision=2, mode='normal'), UUID(first['conversation_id']))
        with pytest.raises(AccessDenied):
            await api.change(world.node, ModeChange(request_id=uuid4(), expected_revision=4, mode='normal'))
        with pytest.raises(ValueError):
            await api.change(world.owner, ModeChange(request_id=uuid4(), expected_revision=4, mode=None))
    asyncio.run(run())


def test_mode_changes_do_not_reuse_previous_work_authority(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        conversation, grant, turn = await setup(app, world, [])
        async with world.db.transaction(write=False) as conn:
            row = await one(conn, 'SELECT work_grant_id FROM chat_turns WHERE turn_id=?', (turn['turn_id'],))
            identifier = row[0]
            assert (await valid_grant(conn, identifier, utc_now()))['permission_mode'] == 'normal'
        await PermissionHTTP(chat, None).change(world.owner, ModeChange(request_id=uuid4(), expected_revision=1, mode='full'))
        async with world.db.transaction(write=False) as conn:
            with pytest.raises(AccessDenied, match='PERMISSION_MODE_CHANGED'):
                await valid_grant(conn, identifier, utc_now())
        # Changes are durable and old grants are not elevated by reopening services.
        reopened = app_for(world, monkeypatch)
        async with reopened.state.chat.database.transaction(write=False) as conn:
            assert (await effective(conn, conversation['conversation_id']))['effective_mode'] == 'full'
    asyncio.run(run())
