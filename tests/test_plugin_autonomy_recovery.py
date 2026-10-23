"""Regression tests for bounded scheduling and explicit, evidence-safe resumption."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from starlette.requests import Request

from nexus.contracts.chat import ConversationCreate
from nexus.core.plugins import lifecycle, schedule
from nexus.core.plugins.http import PluginHTTP
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one
from nexus.core.storage.tasks import Conflict
from test_chat_skills import app_for
from test_chat_work import WorkProvider, setup
from test_plugin_lifecycle import full


async def control(chat, owner, conversation_id, goal_id, state):
    class Guards:
        async def owner_http(self, request):
            return owner
    content = json.dumps({'request_id': str(uuid4()), 'changes': {'status': state}}).encode()
    async def receive():
        return {'type': 'http.request', 'body': content, 'more_body': False}
    request = Request({'type': 'http', 'method': 'PATCH', 'headers': []}, receive)
    return await PluginHTTP(chat.plugins, Guards()).control_record(UUID(conversation_id), UUID(goal_id), request, 'goal')


async def goal_ready(app, world):
    chat = app.state.chat
    chat.plugins.config.enabled = True
    await chat.plugins.seed()
    await full(chat, world.owner)
    conversation, _, _ = await setup(app, world, [{'intent': 'plugin_execute', 'arguments': {
        'plugin_id': 'builtin.goal', 'tool': 'create_goal', 'input': {'objective': '检查已有证据', 'max_rounds': 3}}}])
    for _ in range(4):
        await chat.process_one()
    await lifecycle.tick(chat.plugins)
    async with world.db.transaction(write=False) as conn:
        goal = await one(conn, 'SELECT * FROM plugin_goals')
        turn = await one(conn, 'SELECT t.* FROM chat_turns t JOIN plugin_wakeups w USING(turn_id)')
        assert turn['status'] == 'queued'
    return conversation, goal, turn


@pytest.mark.parametrize('max_runs', [1, 2])
def test_annual_cron_finishes_without_stopping_other_schedules(world, monkeypatch, max_runs):
    app = app_for(world, monkeypatch)
    async def run():
        service = app.state.chat.plugins
        service.config.enabled = True
        conversation = await service.chat.create(world.owner, ConversationCreate(request_id=uuid4()))
        now = datetime(2026, 10, 23, tzinfo=UTC)
        async with world.db.transaction() as conn:
            item = await schedule.manage(conn, world.owner, conversation['conversation_id'], 'schedule_create', {
                'prompt': 'annual reminder', 'cron': '0 9 24 10 *', 'max_runs': max_runs}, now)
            other = await schedule.manage(conn, world.owner, conversation['conversation_id'], 'schedule_create', {
                'prompt': 'other reminder', 'at': item['next_at']}, now)
        due = datetime.fromisoformat(item['next_at'])
        monkeypatch.setattr(schedule, 'utc_now', lambda: due)
        # Exercise the actual worker twice without a wall-clock wait.
        class Finished(Exception): pass
        sleeps = 0
        async def sleep(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2: raise Finished
        monkeypatch.setattr(schedule.asyncio, 'sleep', sleep)
        with pytest.raises(Finished):
            await schedule.run(service)
        async with world.db.transaction(write=False) as conn:
            rows = await conn.execute_fetchall('SELECT schedule_id,runs,paused FROM plugin_schedules')
            assert {r['schedule_id'] for r in rows} == {item['schedule_id'], other['schedule_id']}
            assert all((r['runs'], r['paused']) == (1, 1) for r in rows)
            assert (await one(conn, 'SELECT count(*) FROM plugin_schedule_events'))[0] == 2
    asyncio.run(run())


def test_cron_validation_and_finite_search():
    now = datetime(2026, 10, 24, 1, tzinfo=UTC)
    assert schedule.next_time({'cron': '0 9 24 10 *'}, now) is None
    with pytest.raises(ValueError, match='out of range'):
        schedule.next_time({'cron': '60 9 * * *'}, now)


def test_explicit_goal_resume_creates_one_new_round_and_keeps_root_budget(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        conversation, goal, old = await goal_ready(app, world)
        assert (await control(chat, world.owner, conversation['conversation_id'], goal['goal_id'], 'active'))['status'] == 'active'
        async with world.db.transaction(write=False) as conn:
            before = await lifecycle.budget(conn, chat, old['turn_id'], 50000)
            root = (await one(conn, 'SELECT root_turn_id FROM plugin_authorities'))[0]
        await control(chat, world.owner, conversation['conversation_id'], goal['goal_id'], 'paused')
        assert (await control(chat, world.owner, conversation['conversation_id'], goal['goal_id'], 'active'))['status'] == 'active'
        await lifecycle.tick(chat.plugins)
        await lifecycle.tick(chat.plugins)
        async with world.db.transaction(write=False) as conn:
            wakeups = await conn.execute_fetchall('SELECT * FROM plugin_wakeups ORDER BY rowid')
            assert len(wakeups) == 2 and wakeups[0]['turn_id'] == old['turn_id']
            assert wakeups[1]['turn_id'] != old['turn_id']
            assert (await one(conn, 'SELECT root_turn_id FROM plugin_authorities'))[0] == root
            assert await lifecycle.budget(conn, chat, wakeups[1]['turn_id'], 50000) == before < 50000
            stopped = await one(conn, 'SELECT status,error_code FROM chat_turns WHERE turn_id=?', (old['turn_id'],))
            assert tuple(stopped) == ('stopped', 'PLUGIN_AUTONOMY_PAUSED')
        for _ in range(3):
            await chat.process_one()
        await lifecycle.tick(chat.plugins)
        async with world.db.transaction(write=False) as conn:
            assert tuple(await one(conn, 'SELECT rounds,status FROM plugin_goals')) == (3, 'paused')
            assert (await one(conn, 'SELECT count(*) FROM plugin_wakeups'))[0] == 2
            assert (await one(conn, 'SELECT count(*) FROM model_calls WHERE chat_turn_id=? OR work_root_turn_id=?', (old['turn_id'], old['turn_id'])))[0] == 0
    asyncio.run(run())


@pytest.mark.parametrize('hazard', ['unknown_cost', 'failed', 'recovery', 'expired', 'scope'])
def test_resume_rejects_unsafe_or_ended_goal_atomically(world, monkeypatch, hazard):
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        conversation, goal, old = await goal_ready(app, world)
        await control(chat, world.owner, conversation['conversation_id'], goal['goal_id'], 'paused')
        async with world.db.transaction() as conn:
            if hazard == 'unknown_cost':
                await conn.execute("UPDATE model_calls SET cost_status='unknown'")
            elif hazard == 'failed':
                await conn.execute("UPDATE chat_turns SET error_code='MODEL_RESPONSE_INVALID' WHERE turn_id=?", (old['turn_id'],))
            elif hazard == 'recovery':
                await conn.execute('UPDATE chat_turns SET recovery_required=1 WHERE turn_id=?', (old['turn_id'],))
            elif hazard == 'expired':
                await conn.execute("UPDATE plugin_goals SET expires_at='2020-01-01T00:00:00.000000+00:00'")
            else:
                await conn.execute("UPDATE plugin_authorities SET scope_digest=?", ('0' * 64,))
        with pytest.raises((AccessDenied, Conflict)):
            await control(chat, world.owner, conversation['conversation_id'], goal['goal_id'], 'active')
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, 'SELECT status FROM plugin_goals'))[0] == 'paused'
            assert (await one(conn, 'SELECT state FROM plugin_authorities'))[0] == 'paused'
            assert (await one(conn, 'SELECT count(*) FROM plugin_wakeups'))[0] == 1
    asyncio.run(run())


@pytest.mark.parametrize('unknown', [False, True])
def test_resume_waits_for_real_execution_to_stop(world, monkeypatch, unknown):
    from nexus.core.storage.execution import ExecutionRepository
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        for identity in (world.owner_id, world.node_id):
            await world.security.set_grant(identity, 'system.info')
        conversation, goal, old = await goal_ready(app, world)
        chat.provider = WorkProvider([{'intent': 'get_system_info', 'arguments': {}}])
        for _ in range(2):
            await chat.process_one()
        async with world.db.transaction(write=False) as conn:
            task = await one(conn, 'SELECT task_id FROM chat_work_tasks')
        assert task is not None
        repo = ExecutionRepository(world.db, world.security, 32768)
        await repo.dispatch(UUID(task[0]), world.node, chat.harness.scope(world.node_id), uuid4(), uuid4(), 10)
        await control(chat, world.owner, conversation['conversation_id'], goal['goal_id'], 'paused')
        if unknown:
            async with world.db.transaction() as conn:
                await conn.execute("UPDATE tasks SET execution_outcome='unknown' WHERE task_id=?", (task[0],))
        with pytest.raises((AccessDenied, Conflict)):
            await control(chat, world.owner, conversation['conversation_id'], goal['goal_id'], 'active')
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, 'SELECT count(*) FROM plugin_wakeups'))[0] == 1
            assert (await one(conn, 'SELECT count(*) FROM task_attempts'))[0] == 1
    asyncio.run(run())


def test_stale_schedule_events_do_not_starve_fresh_events(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        chat.plugins.config.enabled = True
        await chat.plugins.seed()
        await full(chat, world.owner)
        await setup(app, world, [{'intent': 'plugin_execute', 'arguments': {
            'plugin_id': 'builtin.schedule', 'tool': 'schedule_create', 'input': {
                'prompt': 'periodic check', 'execute': True, 'every_seconds': 60, 'max_runs': 10}}}])
        for _ in range(4):
            await chat.process_one()
        async with world.db.transaction(write=False) as conn:
            item = await one(conn, 'SELECT * FROM plugin_schedules')
        due = datetime.fromisoformat(item['next_at'])
        for minute in range(7):
            await schedule.tick(chat.plugins, due + timedelta(minutes=minute))
        monkeypatch.setattr(lifecycle, 'utc_now', lambda: due + timedelta(minutes=6))
        await lifecycle.tick(chat.plugins)
        await lifecycle.tick(chat.plugins)
        async with world.db.transaction(write=False) as conn:
            events = await conn.execute_fetchall('SELECT * FROM plugin_schedule_events ORDER BY rowid')
            assert [r['status'] for r in events] == ['missed'] + ['due'] * 6
            wakeups = await conn.execute_fetchall('SELECT * FROM plugin_wakeups')
            assert len(wakeups) == 1 and wakeups[0]['wakeup_id'] == events[1]['event_id']
    asyncio.run(run())
