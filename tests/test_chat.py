"""Multi-turn privacy, shared budgets, real stream cancellation and recovery."""

import asyncio
import copy
import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from nexus.contracts.chat import ChatSubmission, ConversationCreate
from nexus.core.app import create_app
from nexus.core.models.chat import DeepSeekChatProvider
from nexus.core.models.config import ModelConfig
from nexus.core.models.deepseek import ProviderFailure
from nexus.core.models.providers import ModelReply
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict
from test_harness_http import login
from test_language import FakeProvider, rows, submitted
from test_node_link import ORIGIN, core_config


class FakeChat:
    def __init__(self, *, block=False, fail=False, text="这是建议一。\n这是建议二。", reason="stop"):
        self.requests = []
        self.block, self.fail, self.text, self.reason = block, fail, text, reason
        self.started, self.closed = asyncio.Event(), False

    async def invoke(self, messages, on_delta, **options):
        self.requests.append(copy.deepcopy(messages))
        try:
            await on_delta(self.text[:4])
            self.started.set()
            if self.block:
                await asyncio.Event().wait()
            if self.fail:
                raise ProviderFailure("simulated interruption")
            await on_delta(self.text[4:])
            return ModelReply(self.text, 100, 20, self.reason == "stop", finish_reason=self.reason)
        finally:
            self.closed = True


def app_for(world, monkeypatch, provider=None, **limits):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-no-network")
    app = create_app(core_config(world, models=ModelConfig(enabled=True, chat_enabled=True, **limits)))
    app.state.chat.provider = provider or FakeChat()
    app.state.language.provider = FakeProvider()
    return app


async def conversation(chat, world):
    return await chat.create(world.owner, ConversationCreate(request_id=uuid4()))


async def send(chat, world, identifier, **changes):
    history = await chat.history(world.owner, identifier)
    value = dict(request_id=uuid4(), expected_revision=history["conversation"]["revision"], text="我想做一个游戏，给我两个建议", allow_model=True)
    value.update(changes)
    submission = ChatSubmission(**value)
    return await chat.submit(world.owner, identifier, submission), submission


def test_followup_context_is_server_owned_and_no_tasks_or_memories(world, monkeypatch):
    chat = app_for(world, monkeypatch).state.chat
    async def scenario():
        first = await conversation(chat, world)
        turn, submission = await send(chat, world, first["conversation_id"])
        assert await chat.submit(world.owner, first["conversation_id"], submission) == turn
        await chat.process_one()
        second, _ = await send(chat, world, first["conversation_id"], text="把第二个建议展开")
        assert second["context_count"] == 1
        await chat.process_one()
        assert [m["role"] for m in chat.provider.requests[1]] == ["user", "assistant", "user"]
        assert chat.provider.requests[1][1]["content"] == chat.provider.text
        third, _ = await send(chat, world, first["conversation_id"], include_context=False)
        await chat.process_one()
        assert third["context_count"] == 0 and len(chat.provider.requests[2]) == 1
        other = await conversation(chat, world)
        isolated, _ = await send(chat, world, other["conversation_id"])
        assert isolated["context_count"] == 0
        assert len(await rows(world)) == 3
        assert await rows(world, "tasks") == [] and await rows(world, "memory_entries") == []
        assert (await chat.get(world.owner, UUID(turn["turn_id"])))["status"] == "completed"
    asyncio.run(scenario())


def test_consent_revision_concurrency_and_identity_guards(world, monkeypatch):
    chat = app_for(world, monkeypatch).state.chat
    async def scenario():
        conv = await conversation(chat, world)
        with pytest.raises(AccessDenied):
            await send(chat, world, conv["conversation_id"], allow_model=False)
        with pytest.raises(Conflict):
            await send(chat, world, conv["conversation_id"], expected_revision=999)
        turn, value = await send(chat, world, conv["conversation_id"])
        with pytest.raises(Conflict):
            await send(chat, world, conv["conversation_id"])
        with pytest.raises(Conflict):
            await chat.submit(world.owner, conv["conversation_id"], value.model_copy(update={"text": "changed"}))
        with pytest.raises(AccessDenied):
            await chat.get(world.node, UUID(turn["turn_id"]))
        with pytest.raises(LookupError):
            await chat.history(world.owner, uuid4())
        assert len(await rows(world, "chat_turns")) == 1
        assert await rows(world) == []
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["individual", "global", "shutdown"])
def test_partial_output_and_actual_stop_preserve_unknown_reservation(world, monkeypatch, mode):
    provider = FakeChat(block=True)
    chat = app_for(world, monkeypatch, provider).state.chat
    async def scenario():
        conv = await conversation(chat, world)
        turn, _ = await send(chat, world, conv["conversation_id"])
        worker = asyncio.create_task(chat.process_one())
        await asyncio.wait_for(provider.started.wait(), 3)
        partial = await chat.get(world.owner, UUID(turn["turn_id"]))
        assert partial["status"] == "generating" and partial["response_text"] == provider.text[:4]
        if mode == "global":
            await chat.harness.control(world.owner, "cancel_all", uuid4())
        elif mode == "shutdown":
            worker.cancel()
        else:
            await chat.cancel(world.owner, UUID(turn["turn_id"]))
        await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 3)
        result = await chat.get(world.owner, UUID(turn["turn_id"]))
        assert provider.closed and chat.active is None
        assert result["status"] == ("interrupted" if mode == "shutdown" else "stopped")
        assert result["response_text"] == provider.text[:4]
        assert (await rows(world))[0]["cost_status"] == "unknown"
        assert (await rows(world))[0]["reserved_microunits"] == 50000
        await chat.process_one()
        assert len(provider.requests) == 1
    asyncio.run(scenario())


def test_chat_and_language_share_reservations_across_days(world, monkeypatch):
    app = app_for(world, monkeypatch, FakeChat(fail=True), daily_cost_microunits=50000)
    chat, language = app.state.chat, app.state.language
    async def scenario():
        conv = await conversation(chat, world)
        await send(chat, world, conv["conversation_id"])
        await chat.process_one()
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE model_calls SET created_at=?", (timestamp(utc_now()-timedelta(days=2)),))
        await chat.recover()
        item = await language.submit(world.owner, submitted(world))
        await language.process_one()
        assert (await language.get(world.owner, UUID(item["language_id"])))["error_code"] == "BUDGET_EXCEEDED"
        assert language.provider.calls == 0 and len(await rows(world)) == 1
    asyncio.run(scenario())


def test_global_model_claims_serialize_chat_and_language(world, monkeypatch):
    app = app_for(world, monkeypatch, FakeChat(block=True))
    chat, language = app.state.chat, app.state.language
    async def scenario():
        conv = await conversation(chat, world)
        turn, _ = await send(chat, world, conv["conversation_id"])
        await language.submit(world.owner, submitted(world))
        chat_item, language_item = await asyncio.gather(chat.claim(), language.claim())
        assert (chat_item is None) != (language_item is None)
        if chat_item:
            chat_item[2].cancel()
            await asyncio.gather(chat_item[2], return_exceptions=True)
            chat.active = None
        else:
            await language.finish(language_item[0], language_item[1], await language_item[2])
            language.active = None
        assert len(await rows(world)) == 1
        await chat.recover()
    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["pause", "price", "commit"])
def test_no_external_call_before_control_budget_and_commit(world, monkeypatch, boundary):
    chat = app_for(world, monkeypatch).state.chat
    async def scenario():
        conv = await conversation(chat, world)
        turn, _ = await send(chat, world, conv["conversation_id"])
        if boundary == "pause":
            await chat.harness.control(world.owner, "pause", uuid4())
        elif boundary == "price":
            chat.config = chat.config.model_copy(update={"price_valid_until": utc_now().date()-timedelta(days=1)})
        else:
            import aiosqlite
            original = aiosqlite.Connection.commit
            async def fail_commit(conn):
                if (await one(conn, "SELECT count(*) FROM model_calls"))[0]:
                    raise sqlite3.OperationalError("controlled commit failure")
                await original(conn)
            monkeypatch.setattr(aiosqlite.Connection, "commit", fail_commit)
        if boundary == "commit":
            with pytest.raises(sqlite3.OperationalError):
                await chat.process_one()
        else:
            await chat.process_one()
        assert chat.provider.requests == [] and await rows(world) == []
        item = await chat.get(world.owner, UUID(turn["turn_id"]))
        assert item["status"] == ("failed" if boundary == "price" else "queued")
    asyncio.run(scenario())


def test_context_clipping_and_incomplete_answers_are_explicit(world, monkeypatch):
    provider = FakeChat(text="a"*16000)
    chat = app_for(world, monkeypatch, provider).state.chat
    async def scenario():
        conv = await conversation(chat, world)
        for _ in range(2):
            await send(chat, world, conv["conversation_id"])
            await chat.process_one()
        third, _ = await send(chat, world, conv["conversation_id"])
        assert third["context_truncated"] and third["context_count"] == 1
        provider.reason = "length"
        await chat.process_one()
        result = await chat.get(world.owner, UUID(third["turn_id"]))
        assert result["status"] == "incomplete" and result["error_code"] == "MODEL_OUTPUT_LIMIT"
        fourth, _ = await send(chat, world, conv["conversation_id"])
        context = (await chat.get(world.owner, UUID(fourth["turn_id"])))["context_messages"]
        assert len(context) == 3  # Incomplete third answer is excluded.
    asyncio.run(scenario())


def test_restart_reconciles_without_resending_and_preserves_history(world, monkeypatch):
    provider = FakeChat(block=True)
    chat = app_for(world, monkeypatch, provider).state.chat
    async def scenario():
        conv = await conversation(chat, world)
        turn, _ = await send(chat, world, conv["conversation_id"])
        item = await chat.claim()
        await asyncio.wait_for(provider.started.wait(), 3)
        item[2].cancel()
        await asyncio.gather(item[2], return_exceptions=True)
        chat.active = None  # Simulate loss of the old process after its HTTP stopped.
        await chat.recover()
        await chat.process_one()
        result = await chat.get(world.owner, UUID(turn["turn_id"]))
        assert result["status"] == "interrupted" and result["response_text"] == provider.text[:4]
        assert len(provider.requests) == 1 and len(await rows(world)) == 1
    asyncio.run(scenario())


def sse(content=None, *, finish=None, usage=None, delta=None):
    value = {"model":"deepseek-flash", "choices":[{"index":0,"delta": delta if delta is not None else ({"content":content} if content is not None else {}), "finish_reason":finish}]}
    if usage is not None:
        value["usage"] = usage
    return ("data: "+json.dumps(value, ensure_ascii=False)+"\r\n\r\n").encode()


class Stream(httpx2.AsyncByteStream):
    def __init__(self, chunks, *, block=False):
        self.chunks, self.block, self.closed = chunks, block, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.block:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def test_actual_sse_parser_splits_utf8_validates_usage_and_minimizes_request():
    captured, parts = [], []
    first = sse("你好")
    split = first.index("你".encode())+1
    stream = Stream([first[:split],first[split:split+1],first[split+1:],sse(finish="stop",usage={"prompt_tokens":50,"completion_tokens":2}), b"data: [DONE]\n\n"])
    async def handle(request):
        captured.append(json.loads(request.content))
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        return httpx2.Response(200,headers={"content-type":"text/event-stream"},stream=stream)
    async def scenario():
        provider = DeepSeekChatProvider(SecretStr("synthetic-key"), ModelConfig(), transport=httpx2.MockTransport(handle))
        async def receive(value):
            parts.append(value)
        reply = await provider.invoke([{"role":"user","content":"你好"}], receive)
        assert reply.content == "你好" and reply.complete and reply.input_tokens == 50
    asyncio.run(scenario())
    assert stream.closed and parts == ["你好"]
    assert captured[0]["stream"] and "tools" not in captured[0]
    assert captured[0]["thinking"] == {"type":"disabled"}
    assert [m["role"] for m in captured[0]["messages"]] == ["system","user"]


@pytest.mark.parametrize("chunks", [
    [sse("partial"), b"data: [DONE]\n\n"],
    [sse(delta={"tool_calls":[{"name":"shell"}]})],
    [sse("x"*17000)],
    [sse(finish="stop",usage={"prompt_tokens":True,"completion_tokens":1}),b"data: [DONE]\n\n"],
    [b"data: not-json\n\n"],
])
def test_bad_streams_fail_closed_without_leaking_remote_body(chunks):
    async def handle(request):
        return httpx2.Response(200,headers={"content-type":"text/event-stream"},stream=Stream(chunks))
    async def scenario():
        provider = DeepSeekChatProvider(SecretStr("synthetic-key"),ModelConfig(),transport=httpx2.MockTransport(handle))
        async def receive(value):
            pass
        with pytest.raises(ProviderFailure, match="MODEL_REQUEST_FAILED"):
            await provider.invoke([{"role":"user","content":"hello"}],receive)
    asyncio.run(scenario())


def test_http_stream_cancellation_closes_actual_response():
    stream = Stream([sse("开始")],block=True)
    async def handle(request):
        return httpx2.Response(200,headers={"content-type":"text/event-stream"},stream=stream)
    async def scenario():
        provider = DeepSeekChatProvider(SecretStr("synthetic-key"),ModelConfig(),transport=httpx2.MockTransport(handle))
        started = asyncio.Event()
        async def receive(value):
            started.set()
        task = asyncio.create_task(provider.invoke([{"role":"user","content":"hi"}],receive))
        await asyncio.wait_for(started.wait(),2)
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        assert stream.closed
    asyncio.run(scenario())


def test_http_security_validation_background_worker_and_stop(world, monkeypatch):
    import time
    app = app_for(world, monkeypatch, FakeChat(block=True))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/chat/conversations").status_code == 401
        auth = login(client, world)
        assert client.post("/api/v1/chat/conversations",json={"request_id":str(uuid4())}).status_code == 403
        conv = client.post("/api/v1/chat/conversations",json={"request_id":str(uuid4())},headers=auth).json()["conversation"]
        endpoint = f"/api/v1/chat/conversations/{conv['conversation_id']}/turns"
        payload = dict(request_id=str(uuid4()),expected_revision=conv["revision"],text="给我建议",allow_model=True)
        assert client.post(endpoint,json={**payload,"role":"system"},headers=auth).status_code == 400
        assert client.post(endpoint,json={**payload,"allow_model":False},headers=auth).status_code == 403
        turn = client.post(endpoint,json=payload,headers=auth).json()["turn"]
        for _ in range(100):
            value = client.get(f"/api/v1/chat/turns/{turn['turn_id']}").json()["turn"]
            if value["response_text"]:
                break
            time.sleep(.02)
        assert value["status"] == "generating" and value["response_text"]
        assert client.post(f"/api/v1/chat/turns/{turn['turn_id']}/stop",json={},headers=auth).status_code == 200
        for _ in range(100):
            value = client.get(f"/api/v1/chat/turns/{turn['turn_id']}").json()["turn"]
            if value["status"] == "stopped":
                break
            time.sleep(.02)
        assert value["status"] == "stopped" and value["cost_status"] == "unknown"
        assert client.get("/api/v1/models/usage").json()["chat_enabled"]


def test_strict_inputs_and_separate_chat_output_limit():
    for changes in ({"role":"system"},{"expected_revision":True},{"text":" "},{"allow_model":"yes"}):
        with pytest.raises(ValidationError):
            ChatSubmission(**{**dict(request_id=uuid4(),expected_revision=1,text="hello"),**changes})
    assert ModelConfig(chat_enabled=True).max_output_tokens == 512
    with pytest.raises(ValidationError):
        ModelConfig(chat_max_output_tokens=2049)


def test_v6_upgrade_keeps_nonempty_ledger_and_consistent_backup(tmp_path):
    import hashlib
    from importlib.resources import files
    from nexus.core.storage.database import Database
    path = tmp_path/"core.sqlite3"
    now = timestamp(utc_now())
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)")
        for resource in sorted(files("nexus.core.storage").joinpath("migrations").iterdir(),key=lambda p:p.name):
            if resource.name.endswith(".sql") and int(resource.name[:3]) <= 6:
                source=resource.read_text(encoding="utf-8")
                conn.executescript(source)
                conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)",(int(resource.name[:3]),hashlib.sha256(source.encode()).hexdigest(),now))
                conn.commit()
        conn.execute("INSERT INTO model_calls(call_id,provider,model,cost_status,created_at,reserved_microunits) VALUES(?,'deepseek','historical','unknown',?,50000)",(str(uuid4()),now))
        expected = conn.execute("SELECT * FROM model_calls").fetchone()
        conn.commit()
    asyncio.run(Database(path).initialize())
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT * FROM model_calls").fetchone() == (*expected,None,0)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    backups = list(tmp_path.glob("core.sqlite3.before-v7-*.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT * FROM model_calls").fetchone() == expected
