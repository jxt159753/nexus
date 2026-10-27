"""Conversation counts do not block creation, paging or archive restoration."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from nexus.contracts.chat import ConversationUpdate
from nexus.core.storage import archive
from nexus.core.storage.database import one, timestamp, utc_now
from test_autonomy_storage import completed
from test_chat import app_for
from test_chat_skills import app_for as work_app
from test_harness_http import login
from test_node_link import ORIGIN


async def seed_conversations(world, count=100):
    async with world.db.transaction() as conn:
        await conn.executemany(
            "INSERT INTO conversations(conversation_id,owner_id,request_id,title,created_at) VALUES(?,?,?,?,?)",
            [(str(uuid4()), str(world.owner_id), str(uuid4()), f"历史会话 {i}", timestamp(utc_now())) for i in range(count)],
        )


def test_create_above_100_keeps_history_idempotency_and_pagination(world, monkeypatch):
    app = app_for(world, monkeypatch)
    asyncio.run(seed_conversations(world, 105))
    with TestClient(app, base_url=ORIGIN) as client:
        auth = login(client, world)
        payload = {"request_id": str(uuid4())}
        path = "/api/v1/chat/conversations"
        assert client.post(path, json=payload).status_code == 403
        response = client.post(path, json=payload, headers=auth)
        assert response.status_code == 200, response.text
        created = response.json()["conversation"]
        assert client.post(path, json=payload, headers=auth).json()["conversation"] == created
        seen = []
        before = 2**63 - 1
        while True:
            page = client.get(path, params={"before": before}).json()["conversations"]
            assert len(page) <= 20
            seen.extend(row["conversation_id"] for row in page)
            if len(page) < 20:
                break
            before = page[-1]["cursor"]
        assert len(seen) == len(set(seen)) == 106
        target = path + "/" + created["conversation_id"]
        assert client.patch(target, json={"archived": True}, headers=auth).status_code == 200
        archived = client.get(path, params={"archived": True}).json()["conversations"]
        assert [row["conversation_id"] for row in archived] == [created["conversation_id"]]
        assert client.patch(target, json={"archived": False}, headers=auth).status_code == 200
        assert not app.state.chat.provider.requests


def test_cold_archive_restores_with_100_other_conversations(world, monkeypatch):
    app = work_app(world, monkeypatch)
    async def scenario():
        chat = app.state.chat
        conv, turn = await completed(app, world)
        original = await chat.get(world.owner, turn["turn_id"])
        await chat.update(world.owner, conv["conversation_id"], ConversationUpdate(archived=True))
        async with world.db.transaction() as conn:
            row = await chat.conversation(conn, world.owner, conv["conversation_id"])
            preview = await archive.preview(conn, row)
            await archive.archive(conn, row, preview["digest"])
        await seed_conversations(world)
        restored = await chat.update(world.owner, conv["conversation_id"], ConversationUpdate(archived=False))
        assert not restored["cold_archived"] and not restored["archived"]
        assert (await chat.get(world.owner, turn["turn_id"]))["response_text"] == original["response_text"]
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT count(*) FROM conversations"))[0] == 101
            assert (await one(conn, "SELECT count(*) FROM model_calls"))[0] == 1
    asyncio.run(scenario())


def test_conversation_count_alone_does_not_raise_capacity_alert(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def scenario():
        await seed_conversations(world)
        sample = await app.state.chat.plugins.health.sample()
        assert sample["storage"]["hot_conversations"] == 100
        assert sample["status"] == "ok"
        async with world.db.transaction(write=False) as conn:
            assert not (await one(conn, "SELECT active FROM autonomy_alerts WHERE alert_key='capacity'"))[0]
    asyncio.run(scenario())


@pytest.mark.skipif(not shutil.which("node") or not Path("var/attachment-browser/node_modules/playwright-core").exists(), reason="requires existing local Edge test tooling")
def test_conversation_list_browser():
    result = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("conversation_list_browser.cjs"))],
                            capture_output=True, text=True, encoding="utf-8", timeout=90,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"]
