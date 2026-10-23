"""Standing consent, extraction, isolation, revocation and actual SSE framing."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from nexus.contracts.chat import ConversationCreate
from nexus.contracts.records import MemoryCreate, MemoryPolicyUpdate, ProjectCreate
from nexus.core.memory.automatic import AutomaticMemory, recall
from nexus.core.memory.service import Records
from nexus.core.models.chat import DeepSeekChatProvider, build_request
from nexus.core.models.chat_format import ChatText
from nexus.core.models.config import ModelConfig
from nexus.core.storage.database import Database, one, timestamp, utc_now
from test_chat import FakeChat, Stream, app_for, conversation, send, sse
from test_harness_http import login
from test_language import rows
from test_node_link import ORIGIN, core_config
from nexus.core.app import create_app


PREFERENCE = "我喜欢简洁自然的中文回答"


def candidate(quote=PREFERENCE, category="preference", scope="personal"):
    return {"quote": quote, "category": category, "scope": scope}


class MemoryChat(FakeChat):
    def __init__(self, candidates=(), **kwargs):
        super().__init__(**kwargs)
        self.candidates = tuple(candidates)
        self.options = []

    async def invoke(self, messages, on_delta, **options):
        self.options.append(options)
        reply = await super().invoke(messages, on_delta, **options)
        return replace(reply, memory_candidates=self.candidates)


async def enabled(world):
    service = AutomaticMemory(world.db)
    current = await service.get(world.owner)
    return await service.update(world.owner, MemoryPolicyUpdate(expected_revision=current["revision"], enabled=True,
                                                               consent_save=True, consent_model=True))


def test_policy_requires_both_consents_owner_csrf_and_revision(world):
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        assert client.get("/api/v1/memories/policy").status_code == 401
        headers = login(client, world)
        assert client.get("/api/v1/memories/policy").json()["policy"]["enabled"] is False
        value = {"expected_revision": 0, "enabled": True, "consent_save": True, "consent_model": True}
        for key in ("consent_save", "consent_model"):
            assert client.patch("/api/v1/memories/policy", json={**value, key: False}, headers=headers).status_code == 400
        assert client.patch("/api/v1/memories/policy", json=value).status_code == 403
        assert client.patch("/api/v1/memories/policy", json=value, headers=headers).json()["policy"]["revision"] == 1
        assert client.patch("/api/v1/memories/policy", json=value, headers=headers).status_code == 409
        disabled = client.patch("/api/v1/memories/policy", json={"enabled": False, "expected_revision": 1}, headers=headers)
        assert disabled.json()["policy"]["enabled"] is False
    async def check():
        from nexus.core.security.types import AccessDenied
        with pytest.raises(AccessDenied):
            await AutomaticMemory(world.db).get(world.node)
        reopened = Database(world.db.path)
        await reopened.initialize()
        assert (await AutomaticMemory(reopened).get(world.owner))["revision"] == 2
        assert len(await rows(world, "memory_policy_events")) == 2
    asyncio.run(check())


def test_future_only_creation_recall_restart_and_no_extra_calls(world, monkeypatch):
    provider = MemoryChat([candidate()])
    chat = app_for(world, monkeypatch, provider).state.chat
    async def scenario():
        conv = await conversation(chat, world)
        await send(chat, world, conv["conversation_id"], text=PREFERENCE)
        await enabled(world)  # Already queued input predates the consent.
        await chat.process_one()
        assert await rows(world, "memory_entries") == []
        assert not provider.options[0].get("auto_memory")
        turn, submission = await send(chat, world, conv["conversation_id"], text=PREFERENCE)
        assert (await chat.submit(world.owner, conv["conversation_id"], submission))["turn_id"] == turn["turn_id"]
        await chat.process_one()
        saved = await rows(world, "memory_entries")
        assert len(saved) == 1 and saved[0]["content"] == PREFERENCE
        assert saved[0]["verification"] == "unverified"
        assert saved[0]["source"] == "chat:"+turn["turn_id"]
        assert (await chat.get(world.owner, UUID(turn["turn_id"])))["auto_memory_saved_ids"] == [saved[0]["memory_id"]]
        reopened = Database(world.db.path)
        await reopened.initialize()
        assert (await AutomaticMemory(reopened).get(world.owner))["enabled"]
        other = await conversation(chat, world)
        following, _ = await send(chat, world, other["conversation_id"], text="晚上好")
        assert following["auto_memory_references"][0]["memory_id"] == saved[0]["memory_id"]
        await chat.process_one()
        assert len(await rows(world, "memory_entries")) == 1
        assert len(await rows(world)) == 3  # Each ordinary turn still makes one model call.
        assert PREFERENCE in json.dumps(provider.requests[-1], ensure_ascii=False)
    asyncio.run(scenario())


@pytest.mark.parametrize("text,item", [
    ("今天心情不错", candidate()),  # Invented / prior-context quote cannot be stored.
    ("我的密码是 synthetic-only", candidate("我的密码是 synthetic-only", "background")),
    ("我的 API_KEY 是 synthetic-only", candidate("我的 API_KEY 是 synthetic-only", "background")),
    ("不要记住："+PREFERENCE, candidate()),
    ("这是项目的长期目标", candidate("这是项目的长期目标", "ongoing", "project")),
    (PREFERENCE, {**candidate(), "category": "inference"}),
    ("这个项目使用私有部署", candidate("这个项目使用私有部署", "background", "personal")),
    ("引用示例：\n> "+PREFERENCE, candidate()),
    ("示例：\n```text\n"+PREFERENCE+"\n```", candidate()),
])
def test_rejects_unproven_secret_optout_unscoped_or_invalid_candidates(world, monkeypatch, text, item):
    chat = app_for(world, monkeypatch, MemoryChat([item])).state.chat
    async def scenario():
        await enabled(world)
        conv = await conversation(chat, world)
        await send(chat, world, conv["conversation_id"], text=text)
        await chat.process_one()
        assert await rows(world, "memory_entries") == []
    asyncio.run(scenario())


def test_project_isolation_personal_reuse_and_manual_memory_not_enrolled(world, monkeypatch):
    project_quote = "这个项目需要持续跟进部署进度"
    provider = MemoryChat([candidate(), candidate(project_quote, "ongoing", "project")])
    chat = app_for(world, monkeypatch, provider).state.chat
    async def scenario():
        await enabled(world)
        records = Records(world.db)
        first = await records.create_project(world.owner, ProjectCreate(name="一号"))
        second = await records.create_project(world.owner, ProjectCreate(name="二号"))
        conv = await chat.create(world.owner, ConversationCreate(request_id=uuid4(), project_id=UUID(first["project_id"])))
        await records.save(world.owner, MemoryCreate(request_id=uuid4(), consent=True, content="旧手动偏好", kind="fact", source="user"))
        await send(chat, world, conv["conversation_id"], text=PREFERENCE+"。"+project_quote)
        await chat.process_one()
        async with world.db.transaction(write=False) as conn:
            same = await recall(conn, world.owner_id, first["project_id"], "项目部署进度")
            other = await recall(conn, world.owner_id, second["project_id"], "项目部署进度")
            personal = await recall(conn, world.owner_id, None, "项目部署进度")
            foreign = await recall(conn, uuid4(), first["project_id"], "项目部署进度")
        assert {item["content"] for item in same} == {PREFERENCE, project_quote}
        assert [item["content"] for item in other] == [PREFERENCE]
        assert personal == other and foreign == []
        ongoing = next(item for item in same if item["content"] == project_quote)
        assert ongoing["kind"] == "plan" and ongoing["valid_until"]
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["disable", "retract", "correct"])
def test_revocation_blocks_queued_context_and_replayed_history(world, monkeypatch, action):
    provider = MemoryChat([candidate()])
    chat = app_for(world, monkeypatch, provider).state.chat
    async def scenario():
        await enabled(world)
        conv = await conversation(chat, world)
        await send(chat, world, conv["conversation_id"], text=PREFERENCE)
        await chat.process_one()
        old = (await rows(world, "memory_entries"))[0]
        queued, _ = await send(chat, world, conv["conversation_id"], text="继续聊聊")
        if action == "disable":
            await AutomaticMemory(world.db).update(world.owner, MemoryPolicyUpdate(expected_revision=1, enabled=False))
        elif action == "retract":
            await Records(world.db).retract(world.owner, UUID(old["memory_id"]))
        else:
            await Records(world.db).save(world.owner, MemoryCreate(request_id=uuid4(), consent=True, content="我喜欢详细而严谨的解释",
                kind="fact", source="user:correction", supersedes_id=UUID(old["memory_id"])))
        from nexus.core.security.types import AccessDenied
        async with world.db.transaction(write=False) as conn:
            with pytest.raises(AccessDenied, match="MEMORY_UNAVAILABLE"):
                await chat.journal.references(conn, world.owner, None, [old["source"][5:]])
        assert not await chat.process_one()
        assert len(provider.requests) == 1
        assert (await chat.get(world.owner, UUID(queued["turn_id"])))["error_code"] == "MEMORY_UNAVAILABLE"
        following, _ = await send(chat, world, conv["conversation_id"], text="新问题", allow_compaction=True)
        assert following["context_count"] == 0
        assert PREFERENCE not in str(following["auto_memory_references"])
        if action == "correct":
            assert following["auto_memory_references"][0]["content"] == "我喜欢详细而严谨的解释"
        if action == "retract":
            await chat.process_one()
            provider.candidates = (candidate(),)
            await send(chat, world, conv["conversation_id"], text=PREFERENCE)
            await chat.process_one()
            assert len(await rows(world, "memory_entries")) == 1  # No resurrection.
    asyncio.run(scenario())


def test_disabling_during_generation_prevents_save(world, monkeypatch):
    provider = MemoryChat([candidate()], block=True)
    chat = app_for(world, monkeypatch, provider).state.chat
    async def scenario():
        await enabled(world)
        conv = await conversation(chat, world)
        await send(chat, world, conv["conversation_id"], text=PREFERENCE)
        item = await chat.claim()
        row, call_id, task, partial = item
        await provider.started.wait()
        await AutomaticMemory(world.db).update(world.owner, MemoryPolicyUpdate(expected_revision=1, enabled=False))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        from nexus.core.models.providers import ModelReply
        await chat.finish(row, call_id, ModelReply("收到", 100, 20, True, memory_candidates=(candidate(),)), partial())
        assert await rows(world, "memory_entries") == []
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["length", "error"])
def test_incomplete_or_failed_reply_does_not_save(world, monkeypatch, reason):
    chat = app_for(world, monkeypatch, MemoryChat([candidate()], reason=reason)).state.chat
    async def scenario():
        await enabled(world)
        conv = await conversation(chat, world)
        await send(chat, world, conv["conversation_id"], text=PREFERENCE)
        await chat.process_one()
        assert await rows(world, "memory_entries") == []
    asyncio.run(scenario())


@pytest.mark.parametrize("header,expected", [
    ("NEXUS_META "+json.dumps({"memories": [candidate()], "title": "偏好"}, ensure_ascii=False), 1),
    ("\n  NEXUS_META "+json.dumps({"memories": [candidate()]}, ensure_ascii=False), 1),
    ('NEXUS_META {"memories": [{"bogus":true}]}', 0),
    ('NEXUS_META {"memories": [], "memories": []}', 0),
    ('NEXUS_META '+"x"*5000, 0),
])
def test_streamed_metadata_is_bounded_hidden_and_strict(header, expected):
    async def scenario():
        pieces = []
        async def receive(value):
            pieces.append(value)
        parser = ChatText(True, receive, auto_memory=True)
        text = header+"\n自然回答"
        for char in text:
            await parser.feed(char)
        await parser.feed("", final=True)
        assert "".join(pieces) == "自然回答"
        assert len(parser.memories) == expected
    asyncio.run(scenario())


@pytest.mark.parametrize("separator", [" ", "\n", "\r\n"])
def test_actual_provider_metadata_and_request_sizing(separator):
    from nexus.core.cognitive.chat_context import request_size
    from nexus.contracts.extensions import canonical
    async def scenario():
        body = "NEXUS_META"+separator+json.dumps({"memories": [candidate()], "title": "偏好"}, ensure_ascii=False)+"\n好，我们就这样聊。"
        async def handle(request):
            system = json.loads(request.content)["messages"][0]["content"]
            assert "NEXUS_META" in system and "自动记忆当前已开启" in system
            assert "guide an explicit memory request to that editor" not in system
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream([
                sse(body[:15]), sse(body[15:]), sse(finish="stop", usage={"prompt_tokens": 200, "completion_tokens": 80}), b"data: [DONE]\n\n"]))
        provider = DeepSeekChatProvider(SecretStr("synthetic-key"), ModelConfig(), transport=httpx2.MockTransport(handle))
        pieces = []
        async def receive(value):
            pieces.append(value)
        messages = [{"role": "user", "content": PREFERENCE}]
        reply = await provider.invoke(messages, receive, generate_title=True, auto_memory=True)
        assert reply.memory_candidates == (candidate(),) and reply.title == "偏好"
        assert reply.content == "".join(pieces) == "好，我们就这样聊。"
        assert request_size(ModelConfig(), messages, auto_memory=True) == len(canonical(build_request(ModelConfig(), messages, auto_memory=True)).encode())
    asyncio.run(scenario())


@pytest.mark.parametrize("position", ["before", "after", "middle"])
@pytest.mark.parametrize("chunk_size", [1, 7, 10000])
def test_metadata_stays_private_at_any_stream_position(position, chunk_size):
    async def scenario():
        pieces = []
        async def receive(value):
            pieces.append(value)
        parser = ChatText(True, receive, auto_memory=True)
        metadata = 'NEXUS_META\n'+json.dumps({"memories": [candidate()], "title": "偏好"}, ensure_ascii=False, indent=2)+'\n'
        content = {"before": metadata+"自然回答", "after": "自然回答\n"+metadata,
                   "middle": "自然回答\n"+metadata+"后续内容"}[position]
        for index in range(0, len(content), chunk_size):
            await parser.feed(content[index:index+chunk_size])
            assert "NEXUS_META" not in "".join(pieces) and '"memories"' not in "".join(pieces)
        await parser.feed("", final=True)
        assert parser.memories == (candidate(),) and parser.title == "偏好"
        assert "".join(pieces).strip() == ("自然回答\n后续内容" if position == "middle" else "自然回答")
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", ['{\n"memories": [', '{\n"memories": "'+"x"*5000+'"}\n后续文字'])
def test_unterminated_or_oversized_metadata_does_not_leak(payload):
    async def scenario():
        pieces = []
        async def receive(value):
            pieces.append(value)
        parser = ChatText(False, receive, auto_memory=True)
        for char in "自然回答\nNEXUS_META "+payload:
            await parser.feed(char)
        await parser.feed("", final=True)
        assert "".join(pieces) == "自然回答\n" and not parser.memories
        assert len(parser.metadata_buffer) <= 4096
    asyncio.run(scenario())


def test_expiry_propagates_through_history_and_recall_is_bounded(world, monkeypatch):
    provider = MemoryChat([candidate()])
    chat = app_for(world, monkeypatch, provider).state.chat
    async def scenario():
        await enabled(world)
        conv = await conversation(chat, world)
        await send(chat, world, conv["conversation_id"], text=PREFERENCE)
        await chat.process_one()
        expiry = timestamp(utc_now()+timedelta(seconds=10))
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE memory_entries SET valid_until=?", (expiry,))
        second, _ = await send(chat, world, conv["conversation_id"], text="继续")
        await chat.process_one()
        assert second["auto_memory_expires_at"] == expiry
        third, _ = await send(chat, world, conv["conversation_id"], text="请继续", allow_compaction=True)
        assert third["context_count"] == 2 and third["auto_memory_expires_at"] == expiry
        # Advance the validity clock without a real sleep or mutating the user's database.
        import nexus.core.memory.automatic as automatic
        later = utc_now()+timedelta(seconds=11)
        monkeypatch.setattr(automatic, "utc_now", lambda: later)
        assert not await chat.process_one()
        async with world.db.transaction(write=False) as conn:
            assert await recall(conn, world.owner_id, None, "继续") == []
        fresh, _ = await send(chat, world, conv["conversation_id"], text="新的开始", allow_compaction=True)
        assert fresh["context_count"] == 0
        assert (await chat.get(world.owner, UUID(third["turn_id"])))["error_code"] == "MEMORY_UNAVAILABLE"
    asyncio.run(scenario())


def test_auto_memory_retains_work_reply_and_native_proposal(world, monkeypatch):
    from test_chat_continuity import work_input
    from test_chat_work import WorkProvider, basic
    class NativeMemory(WorkProvider):
        async def invoke(self, messages, receive, **options):
            reply = await super().invoke(messages, receive, **options)
            return replace(reply, memory_candidates=(candidate(),))
    app = app_for(world, monkeypatch, NativeMemory([{"intent": "get_system_info", "arguments": {}}]))
    chat = app.state.chat
    async def scenario():
        await basic(app, world)
        await enabled(world)
        conv = await conversation(chat, world)
        turn, _ = await work_input(chat, world, conv["conversation_id"], PREFERENCE+"。查看系统信息")
        await chat.process_one()
        current = await chat.get(world.owner, UUID(turn["turn_id"]))
        assert current["work_steps"] and current["auto_memory_saved_ids"]
        assert len(await rows(world, "memory_entries")) == 1
        assert len(await rows(world)) == 1
    asyncio.run(scenario())


def test_failed_memory_write_rolls_back_consent_and_model_settlement(world, monkeypatch):
    import sqlite3
    chat = app_for(world, monkeypatch, MemoryChat([candidate()])).state.chat
    async def scenario():
        await enabled(world)
        conv = await conversation(chat, world)
        await send(chat, world, conv["conversation_id"], text=PREFERENCE)
        async with world.db.transaction() as conn:
            await conn.execute("CREATE TRIGGER injected BEFORE INSERT ON memory_entries BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(sqlite3.IntegrityError):
            await chat.process_one()
        assert await rows(world, "memory_entries") == []
        assert await rows(world, "memory_consents") == []
        assert (await rows(world))[0]["outcome"] == "started"
    asyncio.run(scenario())


def test_disable_and_reenable_never_reuses_old_context_or_collects_disabled_turn(world, monkeypatch):
    chat = app_for(world, monkeypatch, MemoryChat([candidate()])).state.chat
    async def scenario():
        await enabled(world)
        conv = await conversation(chat, world)
        await send(chat, world, conv["conversation_id"], text=PREFERENCE)
        await chat.process_one()
        await AutomaticMemory(world.db).update(world.owner, MemoryPolicyUpdate(expected_revision=1, enabled=False))
        chat.provider.candidates = (candidate("我喜欢画科幻插画"),)
        await send(chat, world, conv["conversation_id"], text="我喜欢画科幻插画")
        await chat.process_one()
        await enabled(world)
        following, _ = await send(chat, world, conv["conversation_id"], text="你好")
        assert following["context_count"] == 1
        assert following["auto_memory_revision"] == 3
        assert len(following["auto_memory_references"]) == 1
        assert len(await rows(world, "memory_entries")) == 1
    asyncio.run(scenario())


def test_v28_upgrade_preserves_manual_records_and_defaults_disabled(world, tmp_path):
    import hashlib
    import sqlite3
    from contextlib import closing
    from importlib.resources import files
    path = tmp_path / "old.sqlite3"
    now = timestamp(utc_now())
    with closing(sqlite3.connect(path)) as conn, closing(sqlite3.connect(world.db.path)) as source:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)")
        for migration in sorted(files("nexus.core.storage").joinpath("migrations").iterdir(), key=lambda p: p.name):
            if not migration.name.endswith(".sql") or int(migration.name[:3]) > 28:
                continue
            script = migration.read_text(encoding="utf-8")
            conn.executescript(script)
            conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (int(migration.name[:3]), hashlib.sha256(script.encode()).hexdigest(), now))
        identity = source.execute("SELECT * FROM identities WHERE identity_id=?", (str(world.owner_id),)).fetchone()
        conn.execute("INSERT INTO identities VALUES("+",".join("?" for _ in identity)+")", identity)
        consent, memory = str(uuid4()), str(uuid4())
        conn.execute("INSERT INTO memory_consents VALUES(?,?,?,?,?)", (consent, str(world.owner_id), str(uuid4()), "{}", now))
        conn.execute("INSERT INTO memory_entries(memory_id,owner_id,consent_event_id,content,kind,verification,source,created_at) VALUES(?,?,?,?,'fact','unverified','user',?)", (memory, str(world.owner_id), consent, "原有手动记录", now))
        conn.commit()
    async def scenario():
        db = Database(path)
        await db.initialize()
        async with db.transaction(write=False) as conn:
            old = await one(conn, "SELECT * FROM memory_entries WHERE memory_id=?", (memory,))
            assert old["content"] == "原有手动记录" and old["auto_category"] is None
            assert (await one(conn, "PRAGMA quick_check"))[0] == "ok"
        assert (await AutomaticMemory(db).get(world.owner))["enabled"] is False
        backups = list(tmp_path.glob("old.sqlite3.before-v29-*.sqlite3"))
        assert len(backups) == 1
        with closing(sqlite3.connect(backups[0])) as backup:
            assert backup.execute("SELECT content FROM memory_entries WHERE memory_id=?", (memory,)).fetchone()[0] == "原有手动记录"
            assert backup.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 28
    asyncio.run(scenario())
