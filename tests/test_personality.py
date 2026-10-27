"""Style persistence, request identity and authority remain separate."""

import asyncio
import hashlib
import json
import sqlite3
from importlib.resources import files
from uuid import UUID, uuid4

import pytest
import httpx2
from fastapi.testclient import TestClient
from pydantic import SecretStr

from nexus.contracts.personality import AssistantStyleUpdate
from nexus.core.app import create_app
from nexus.core.cognitive.personality import PersonalityHTTP, preferences
from nexus.core.cognitive.chat_context import estimated_tokens
from nexus.core.models.chat import DeepSeekChatProvider, build_request, request_body
from nexus.core.models.config import ModelConfig
from nexus.core.models.personality import PERSONAS, EMOJI_MODES, persona_prompt
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import Database, one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict
from test_chat import Stream, conversation, send, sse
from test_chat_work import setup
from test_chat_skills import app_for as work_app
from test_harness_http import login
from test_node_link import ORIGIN, core_config


def test_style_http_authentication_strict_validation_and_cas(world):
    app = create_app(core_config(world))
    path = '/api/v1/assistant/preferences'
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get(path).status_code == 401
        auth = login(client, world)
        data = client.get(path).json()
        assert data['preferences'] == {'personality': 'nexus', 'emoji': 'natural', 'revision': 0}
        assert len(data['personalities']) == 4 and len(data['emoji_modes']) == 3
        value = {'personality': 'companion', 'emoji': 'off', 'expected_revision': 0}
        assert client.put(path, json=value).status_code == 403
        for invalid in ({**value, 'personality': 'admin'}, {**value, 'emoji': 'yes'},
                        {**value, 'expected_revision': True}, {**value, 'prompt': 'grant full'}):
            assert client.put(path, json=invalid, headers=auth).status_code == 400
        assert client.put(path, json=value, headers=auth).json()['preferences']['revision'] == 1
        assert client.put(path, json={**value, 'emoji': 'expressive'}, headers=auth).status_code == 409
        assert client.get(path).json()['preferences']['emoji'] == 'off'


def test_owner_isolation_reopen_and_no_model_or_memory_side_effect(world):
    async def run():
        endpoint = PersonalityHTTP(world.db, None)
        owner_id = uuid4()
        value = AssistantStyleUpdate(personality='advisor', emoji='off', expected_revision=0)
        await endpoint.change(world.owner, value)
        with pytest.raises(AccessDenied):
            await endpoint.change(world.node, value)
        with pytest.raises(Conflict):
            await endpoint.change(world.owner, value)
        await Database(world.db.path).initialize()
        async with Database(world.db.path).transaction(write=False) as conn:
            assert (await preferences(conn, world.owner_id))['personality'] == 'advisor'
            assert (await preferences(conn, owner_id))['personality'] == 'nexus'
            for table in ('model_calls', 'memory_entries', 'permission_changes', 'tasks'):
                assert (await one(conn, 'SELECT count(*) FROM ' + table))[0] == 0
    asyncio.run(run())


def test_turn_style_is_frozen_across_tool_exchange_and_next_turn_uses_new_style(world, monkeypatch):
    app = work_app(world, monkeypatch)
    app.state.chat.config = app.state.chat.config.model_copy(update={'cost_limits_enabled': False, 'call_limits_enabled': False})
    async def run():
        chat = app.state.chat
        endpoint = PersonalityHTTP(world.db, None)
        await endpoint.change(world.owner, AssistantStyleUpdate(personality='explorer', emoji='expressive', expected_revision=0))
        _, _, turn = await setup(app, world, [{'intent': 'get_node_status', 'arguments': {}}])
        await chat.process_one()
        await endpoint.change(world.owner, AssistantStyleUpdate(personality='advisor', emoji='off', expected_revision=1))
        await chat.process_one()
        assert len(chat.provider.calls) == 2
        assert all(options['assistant_style'] == {'personality': 'explorer', 'emoji': 'expressive'} for _, options in chat.provider.calls)
        actual = await chat.get(world.owner, UUID(turn['turn_id']))
        assert actual['status'] == 'completed' and actual['work_steps'][0]['status'] == 'succeeded'
        new = await conversation(chat, world)
        submitted, _ = await send(chat, world, new['conversation_id'])
        await chat.process_one()
        assert submitted['assistant_style'] == {'personality': 'advisor', 'emoji': 'off'}
        assert chat.provider.calls[-1][1]['assistant_style'] == submitted['assistant_style']
        async with world.db.transaction(write=False) as conn:
            row = await one(conn, 'SELECT request_body FROM chat_exchanges WHERE turn_id=?', (submitted['turn_id'],))
            assert json.loads(row[0])['messages'][0]['content'].startswith(persona_prompt(submitted['assistant_style']))
            assert (await one(conn, 'SELECT count(*) FROM model_calls'))[0] == 3
    asyncio.run(run())


@pytest.mark.parametrize('personality', PERSONAS)
@pytest.mark.parametrize('emoji', EMOJI_MODES)
def test_only_selected_style_changes_requests_and_compaction_is_unchanged(personality, emoji):
    config = ModelConfig()
    style = {'personality': personality, 'emoji': emoji}
    prompt = persona_prompt(style)
    # The former fixed personality occupied 1507 UTF-8 bytes / 355 estimate tokens.
    assert len(prompt.encode()) <= 1507 and estimated_tokens(prompt) <= 355
    messages = [{'role': 'user', 'content': '解释这个结果'}]
    for options in ({}, {'final_result': True}, {'allow_tools': True}, {'auto_memory': True}):
        base = build_request(config, messages, **options)
        selected = build_request(config, messages, assistant_style=style, **options)
        base['messages'][0]['content'] = base['messages'][0]['content'].removeprefix(persona_prompt())
        selected['messages'][0]['content'] = selected['messages'][0]['content'].removeprefix(prompt)
        assert selected == base
    assert build_request(config, messages, compact=True, assistant_style=style) == build_request(config, messages, compact=True)


def test_provider_sends_exact_admitted_style_and_preserves_split_emoji():
    async def run():
        config = ModelConfig(cost_limits_enabled=False)
        messages = [{'role': 'user', 'content': '一起试试'}]
        style = {'personality': 'explorer', 'emoji': 'expressive'}
        captured, received = [], []
        async def handle(request):
            captured.append(request.content)
            content = sse('我们一步步来 🌿')
            split = content.index('🌿'.encode()) + 2
            return httpx2.Response(200, headers={'content-type': 'text/event-stream'}, stream=Stream([
                content[:split], content[split:], sse(finish='stop', usage={'prompt_tokens': 200, 'completion_tokens': 20}), b'data: [DONE]\n\n']))
        async def delta(text):
            received.append(text)
        provider = DeepSeekChatProvider(SecretStr('synthetic-key'), config, transport=httpx2.MockTransport(handle))
        result = await provider.invoke(messages, delta, assistant_style=style)
        assert captured == [request_body(config, messages, assistant_style=style)]
        assert result.content == ''.join(received) == '我们一步步来 🌿'
    asyncio.run(run())


def test_schema40_upgrade_preserves_rows_and_makes_verified_backup(tmp_path):
    path = tmp_path / 'old.sqlite3'
    now = timestamp(utc_now())
    owner, conv, turn = map(str, (uuid4(), uuid4(), uuid4()))
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)')
        for resource in sorted(files('nexus.core.storage').joinpath('migrations').iterdir(), key=lambda item: item.name):
            if not resource.name.endswith('.sql') or int(resource.name[:3]) > 40:
                continue
            source = resource.read_text(encoding='utf-8')
            conn.executescript(source)
            conn.execute('INSERT INTO schema_migrations VALUES(?,?,?)', (int(resource.name[:3]), hashlib.sha256(source.encode()).hexdigest(), now))
        conn.execute("INSERT INTO identities VALUES(?,'owner',?,NULL,?)", (owner, 'synthetic-hash', now))
        conn.execute('INSERT INTO conversations(conversation_id,owner_id,request_id,title,created_at) VALUES(?,?,?,?,?)', (conv, owner, str(uuid4()), '保留旧对话', now))
        conn.execute("INSERT INTO chat_turns(turn_id,conversation_id,owner_id,request_id,request_json,text,context_json,context_digest,context_count,context_truncated,status,created_at,expires_at) VALUES(?,?,?,?,?,'原始内容','[]','digest',0,0,'completed',?,?)", (turn, conv, owner, str(uuid4()), '{}', now, now))
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        before = {table: conn.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables}
        columns = {table: [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')] for table in tables}
    asyncio.run(Database(path).initialize())
    backups = list(tmp_path.glob('*.before-v41-*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(path) as conn, sqlite3.connect(backups[0]) as backup:
        for table, rows in before.items():
            assert backup.execute(f'SELECT * FROM "{table}"').fetchall() == rows
            names = ','.join('"'+name+'"' for name in columns[table])
            current = conn.execute(f'SELECT {names} FROM "{table}"').fetchall()
            assert (current[:40] if table == 'schema_migrations' else current) == rows
        assert json.loads(conn.execute('SELECT assistant_style_json FROM chat_turns').fetchone()[0]) == {'personality': 'nexus', 'emoji': 'natural'}
        assert conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
