"""Lossless archive/readback, durable replay identities and supervised health."""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from importlib.resources import files
from uuid import uuid4

import pytest

from nexus.contracts.chat import ConversationUpdate
from nexus.core.plugins import session_context
from nexus.core.security.types import AccessDenied
from nexus.core.storage import archive
from nexus.core.storage.database import Database, one
from nexus.core.storage.tasks import Conflict
from nexus.node.receipts import ReceiptStore
from test_chat_skills import app_for
from test_chat_work import setup
from test_node_receipts import store_at
from node_support import command


async def completed(app, world):
    conv, _, turn = await setup(app, world, [])
    await app.state.chat.process_one()
    return conv, turn


def test_archive_roundtrip_history_journal_and_identity_preserved(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        conv, turn = await completed(app, world)
        original = await chat.get(world.owner, turn['turn_id'])
        journal = await chat.journal.detail(world.owner, turn['turn_id'])
        await chat.update(world.owner, conv['conversation_id'], ConversationUpdate(archived=True))
        async with world.db.transaction() as conn:
            conversation = await chat.conversation(conn, world.owner, conv['conversation_id'])
            with pytest.raises(Conflict): await archive.archive(conn, conversation, 'f'*64)
            preview = await archive.preview(conn, conversation)
            assert preview['rows'] >= 2
            await archive.archive(conn, conversation, preview['digest'])
            assert (await one(conn, 'SELECT text FROM chat_turns WHERE turn_id=?', (turn['turn_id'],)))[0] == ''
        assert (await chat.get(world.owner, turn['turn_id']))['response_text'] == original['response_text']
        assert await chat.journal.detail(world.owner, turn['turn_id']) == journal
        await world.db.initialize()
        await chat.update(world.owner, conv['conversation_id'], ConversationUpdate(archived=False))
        assert (await chat.get(world.owner, turn['turn_id']))['response_text'] == original['response_text']
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, 'SELECT count(*) FROM cold_payloads'))[0] == 0
            assert (await one(conn, 'SELECT count(*) FROM model_calls'))[0] == 1
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['active', 'unsettled_cost', 'corrupt', 'write_failure'])
def test_archive_rejects_active_unknown_corruption_and_rolls_back(world, monkeypatch, failure):
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat; conv, turn = await completed(app, world)
        await chat.update(world.owner, conv['conversation_id'], ConversationUpdate(archived=True))
        async with world.db.transaction() as conn:
            if failure == 'active': await conn.execute("UPDATE chat_turns SET status='queued' WHERE turn_id=?", (turn['turn_id'],))
            if failure == 'unsettled_cost': await conn.execute("UPDATE model_calls SET cost_status='unknown' WHERE coalesce(chat_turn_id,work_root_turn_id)=?", (turn['turn_id'],))
            conversation = await chat.conversation(conn, world.owner, conv['conversation_id'])
            if failure in {'active', 'unsettled_cost'}:
                with pytest.raises(AccessDenied): await archive.preview(conn, conversation)
                return
            preview = await archive.preview(conn, conversation)
            if failure == 'corrupt':
                await archive.archive(conn, conversation, preview['digest'])
                await conn.execute("UPDATE cold_payloads SET sha256=?", ('0'*64,))
            else:
                await conn.execute("CREATE TRIGGER fail_archive BEFORE UPDATE OF text ON chat_turns BEGIN SELECT RAISE(ABORT, 'injected'); END")
        if failure == 'corrupt':
            with pytest.raises(AccessDenied, match='ARCHIVE_INVALID'): await chat.get(world.owner, turn['turn_id'])
        else:
            with pytest.raises(sqlite3.IntegrityError):
                async with world.db.transaction() as conn: await archive.archive(conn, conversation, preview['digest'])
            async with world.db.transaction(write=False) as conn:
                assert (await one(conn, 'SELECT count(*) FROM cold_payloads'))[0] == 0
                assert (await one(conn, 'SELECT text FROM chat_turns WHERE turn_id=?', (turn['turn_id'],)))[0]
    asyncio.run(run())


def test_session_search_honors_revoked_memory_private_context_and_scope(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        conv, turn = await completed(app, world)
        async with world.db.transaction() as conn:
            conversation = await app.state.chat.conversation(conn, world.owner, conv['conversation_id'])
            assert len(await session_context.search(conn, world.owner, conversation, query='完成')) == 1
            await conn.execute('UPDATE chat_turns SET auto_memory_revision=1 WHERE turn_id=?', (turn['turn_id'],))
            assert await session_context.search(conn, world.owner, conversation, query='完成') == []
            with pytest.raises(AccessDenied): await session_context.visible(conn, world.owner, conversation, turn['turn_id'])
            await conn.execute("UPDATE chat_turns SET auto_memory_revision=0,references_json='[{}]' WHERE turn_id=?", (turn['turn_id'],))
            assert await session_context.search(conn, world.owner, conversation) == []
            assert await session_context.search(conn, world.node, conversation) == []
    asyncio.run(run())


def test_health_alerts_are_transition_only_and_worker_death_stops_new_work(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        health = app.state.chat.plugins.health
        task = asyncio.create_task(asyncio.Event().wait())
        health.workers = {'test_worker': task}
        assert (await health.sample())['status'] == 'ok'
        await health.sample()
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT revision FROM autonomy_alerts WHERE alert_key='workers'"))[0] == 1
        task.cancel(); await asyncio.gather(task, return_exceptions=True)
        assert (await health.sample())['status'] == 'failed'
        assert app.state.chat.failed and app.state.harness.failed
        await health.sample()
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT revision FROM autonomy_alerts WHERE alert_key='workers'"))[0] == 2
    asyncio.run(run())


def test_health_distinguishes_running_attempt_from_unreconciled_result(world, monkeypatch, tmp_path):
    from test_autonomy_goals import ready
    app = app_for(world, monkeypatch)
    async def run():
        _, _, _, _, task_id = await ready(app, world, tmp_path)
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE tasks SET status='running',execution_outcome='unknown' WHERE task_id=?", (task_id,))
            await conn.execute("UPDATE task_attempts SET phase='running' WHERE task_id=?", (task_id,))
        assert (await app.state.chat.plugins.health.sample())['status'] == 'ok'
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE task_attempts SET phase='unknown' WHERE task_id=?", (task_id,))
        result = await app.state.chat.plugins.health.sample()
        assert result['status'] == 'degraded' and result['unknown_tasks'] == 1
    asyncio.run(run())


def test_node_archive_keeps_replay_and_cancellation_fences(tmp_path):
    cfg, store = store_at(tmp_path, max_receipts=1)
    item = command(cfg); store.accept(item, uuid4()); store.finish(item.attempt_id, 'cancelled'); store.acknowledge(item.attempt_id)
    store.accept(command(cfg), uuid4())
    with store.transaction() as conn:
        assert conn.execute('SELECT count(*) FROM receipt_archives').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM execution_receipts').fetchone()[0] == 1
    reopened = ReceiptStore(store.path, cfg.node_id); reopened.initialize()
    assert reopened.accept(item, uuid4()).phase == 'cancelled'
    reopened.cancel_pending(item.attempt_id); reopened.acknowledge(item.attempt_id)
    with reopened.transaction() as conn:
        assert conn.execute('SELECT count(*) FROM cancellation_tombstones').fetchone()[0] == 0
        conn.execute('UPDATE receipt_archives SET sha256=?', ('f'*64,))
    with pytest.raises(RuntimeError, match='integrity'): reopened.get(item.attempt_id)


def test_v29_upgrade_backs_up_and_preserves_existing_rows(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT,applied_at TEXT)')
        for resource in sorted(files('nexus.core.storage').joinpath('migrations').iterdir(), key=lambda x: x.name):
            if resource.name.endswith('.sql') and int(resource.name[:3]) <= 29:
                source = resource.read_text(encoding='utf-8')
                conn.executescript(source)
                conn.execute('INSERT INTO schema_migrations VALUES(?,?,?)', (int(resource.name[:3]), hashlib.sha256(source.encode()).hexdigest(), '2026-10-23'))
                conn.commit()
        before = list(conn.execute('SELECT * FROM policy_state'))
    asyncio.run(Database(path).initialize())
    with closing(sqlite3.connect(path)) as conn:
        assert list(conn.execute('SELECT * FROM policy_state')) == before
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('SELECT count(*) FROM goal_plans').fetchone()[0] == 0
    backups = list(tmp_path.glob('old.sqlite3.before-v30-*'))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as conn:
        assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 29
