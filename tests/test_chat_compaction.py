"""DSH-style checkpoint lifecycle with real storage and bounded fake providers."""

import asyncio
import copy
import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from nexus.contracts.chat import ChatSubmission
from nexus.core.cognitive.chat_context import pressure
from nexus.core.models.config import ModelConfig
from nexus.core.models.providers import ModelReply
from nexus.core.storage.database import one
from test_chat import FakeChat, app_for, conversation, rows, send


SUMMARY = "\n".join(f"## {title}\n- {text}" for title,text in [
    ("Goal","完成用户的项目"),("Decisions and Constraints","保持预算和原权限"),
    ("Facts and Evidence","旧记录保留，执行状态以工具证据为准"),("Completed Work","讨论过项目方向"),
    ("Pending Work","继续当前请求"),("Next Step","核对用户最新要求")])


class CompactProvider(FakeChat):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.options = []

    async def invoke(self, messages, on_delta, **options):
        self.options.append(copy.deepcopy(options))
        if not options.get("compact"):
            return await super().invoke(messages,on_delta,**options)
        self.requests.append(copy.deepcopy(messages))
        self.started.set()
        await on_delta(SUMMARY[:8])
        if self.block:
            try:
                await asyncio.Event().wait()
            finally:
                self.closed = True
        content = self.text if self.fail else SUMMARY
        return ModelReply(content,1000,150,True,finish_reason="stop")


async def seed(chat, world, count=4):
    conv = await conversation(chat,world)
    ids = []
    for index in range(count):
        turn,_ = await send(chat,world,conv["conversation_id"],text=f"项目方向 {index}")
        await chat.process_one()
        ids.append(turn["turn_id"])
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE chat_turns SET response_text=? WHERE turn_id=?",(f"决定{index}。"+"a"*4000,turn["turn_id"]))
    return conv,ids


def test_model_window_output_headroom_and_transport_all_constrain_pressure():
    small=ModelConfig(chat_context_window_tokens=4096,chat_input_tokens=3000,chat_max_output_tokens=1024)
    data=pressure(small,[{"role":"user","content":"中"*1800}])
    assert data["threshold_tokens"] <= 2560
    assert pressure(ModelConfig(),[{"role":"user","content":"x"*33000}])["fits"] is False
    with pytest.raises(ValidationError):
        ModelConfig(chat_context_window_tokens=4096,chat_compaction_headroom_tokens=4096)


def test_automatic_compaction_preserves_raw_history_pairs_and_journal(world,monkeypatch):
    provider=CompactProvider()
    chat=app_for(world,monkeypatch,provider,chat_input_bytes=16384).state.chat
    async def run():
        conv,ids=await seed(chat,world)
        turn,request=await send(chat,world,conv["conversation_id"],text="继续最新目标",allow_compaction=True)
        raw=(await chat.get(world.owner,turn["turn_id"]))["context_messages"]
        assert raw[-1]["content"]==request.text and len(raw)==9
        await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        assert current["status"]=="queued" and current["response_text"]==""
        assert current["context_messages"]==raw and len(current["effective_context_messages"])<len(raw)
        await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="completed" and done["model_call_count"]==2
        journal=await chat.journal.detail(world.owner,turn["turn_id"])
        comp=journal["compactions"][0]
        assert comp["outcome"]=="completed" and comp["after_tokens"]<comp["before_tokens"]
        assert json.loads(comp["source_ids_json"])==ids[:3]
        for exchange in journal["exchanges"]:
            assert json.loads(exchange["request_body"])["messages"][-1]["role"]=="user"
        follow,_=await send(chat,world,conv["conversation_id"],allow_compaction=True)
        context=(await chat.get(world.owner,follow["turn_id"]))["context_messages"]
        assert "<compacted-summary>" in context[0]["content"]
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn,"SELECT response_text FROM chat_turns WHERE turn_id=?",(ids[0],)))[0].endswith("a"*4000)
    asyncio.run(run())


def test_manual_compact_is_one_call_idempotent_and_survives_recovery(world,monkeypatch):
    provider=CompactProvider()
    chat=app_for(world,monkeypatch,provider).state.chat
    async def run():
        conv,ids=await seed(chat,world,2)
        turn,request=await send(chat,world,conv["conversation_id"],text="/compact",allow_compaction=True,compact_only=True)
        await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="completed" and done["model_call_count"]==1 and SUMMARY in done["response_text"]
        assert (await chat.submit(world.owner,conv["conversation_id"],request))["turn_id"]==turn["turn_id"]
        count=len(provider.requests)
        await chat.recover()
        await chat.process_one()
        assert len(provider.requests)==count
        follow,_=await send(chat,world,conv["conversation_id"],allow_compaction=True)
        raw=(await chat.get(world.owner,follow["turn_id"]))["context_messages"]
        assert len(raw)==2 and "原文引用 ctx:" in raw[0]["content"]
        async with world.db.transaction(write=False) as conn:
            saved = await one(conn,"SELECT source_ids_json FROM chat_compactions WHERE turn_id=?",(turn["turn_id"],))
            assert json.loads(saved[0]) == ids
    asyncio.run(run())


@pytest.mark.parametrize("boundary",["stop","restart","failure","not_smaller"])
def test_failed_or_interrupted_summary_never_replaces_source_or_retries(world,monkeypatch,boundary):
    provider=CompactProvider()
    chat=app_for(world,monkeypatch,provider).state.chat
    async def run():
        conv,_=await seed(chat,world,2)
        turn,_=await send(chat,world,conv["conversation_id"],text="/compact",compact_only=True,allow_compaction=True)
        raw=(await chat.get(world.owner,turn["turn_id"]))["context_messages"]
        if boundary in {"stop","restart"}:
            provider.block=True;provider.started.clear()
            item=await chat.claim()
            await asyncio.wait_for(provider.started.wait(),2)
            if boundary=="stop":
                await chat.cancel(world.owner,UUID(turn["turn_id"]))
                await asyncio.gather(item[2],return_exceptions=True)
                await chat.finish(item[0],item[1],None,item[3]())
            else:
                item[2].cancel();await asyncio.gather(item[2],return_exceptions=True)
                await chat.recover()
            chat.active=None
        else:
            provider.fail=True;provider.text=SUMMARY+"b"*20000 if boundary=="not_smaller" else "缺少结构的摘要"
            await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"] in {"failed","stopped","interrupted"}
        assert done["effective_context_messages"]==raw
        count=len(provider.requests)
        await chat.process_one()
        assert len(provider.requests)==count
        journal=await chat.journal.detail(world.owner,turn["turn_id"])
        assert journal["compactions"][0]["outcome"]!="completed"
    asyncio.run(run())


def test_without_consent_and_with_context_disabled_no_auxiliary_call(world,monkeypatch):
    provider=CompactProvider()
    chat=app_for(world,monkeypatch,provider).state.chat
    async def run():
        conv,_=await seed(chat,world,2)
        turn,_=await send(chat,world,conv["conversation_id"],allow_compaction=True,include_context=False)
        await chat.process_one()
        assert (await chat.get(world.owner,turn["turn_id"]))["model_call_count"]==1
        assert not any(options.get("compact") for options in provider.options)
    asyncio.run(run())


def test_empty_manual_compaction_is_free_and_legacy_schema_still_works(world,monkeypatch):
    chat=app_for(world,monkeypatch).state.chat
    async def run():
        conv=await conversation(chat,world)
        turn,_=await send(chat,world,conv["conversation_id"],text="/compact",allow_compaction=True,compact_only=True)
        assert not await chat.process_one()
        assert not await rows(world)
        assert (await chat.get(world.owner,turn["turn_id"]))["status"]=="completed"
    asyncio.run(run())


def test_compaction_shares_work_budget_and_preserves_following_native_pair(world,monkeypatch):
    from nexus.contracts.work import WorkGrantCreate
    from nexus.core.cognitive import work_catalog
    from test_chat_work import WorkProvider
    provider=CompactProvider()
    app=app_for(world,monkeypatch,provider,chat_input_bytes=16384)
    chat=app.state.chat
    async def run():
        conv,_=await seed(chat,world)
        grant=await chat.work.grant(world.owner,conv["conversation_id"],WorkGrantCreate(request_id=uuid4(),node_id=world.node_id,consent=True))
        async with world.db.transaction(write=False) as conn:
            catalog=await work_catalog.catalog(chat,conn,world.owner,world.node_id)
        native=WorkProvider([{"intent":"get_node_status","arguments":{}}])
        async def mixed(messages,on_delta,**options):
            return await (provider.invoke(messages,on_delta,**options) if options.get("compact") else native.invoke(messages,on_delta,**options))
        class Mixed:
            invoke=staticmethod(mixed)
        chat.provider=Mixed()
        turn,_=await send(chat,world,conv["conversation_id"],allow_compaction=True,allow_tools=True,node_id=world.node_id,
                          work_grant_id=UUID(grant["grant_id"]),work_digest=catalog["work_digest"],allow_work_results=True)
        for _ in range(3):
            assert await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="completed" and done["model_call_count"]==3 and done["work_round"]==1
        calls=[m for m in done["work_messages"] if m.get("tool_calls")]
        results=[m for m in done["work_messages"] if m["role"]=="tool"]
        assert calls[0]["tool_calls"][0]["id"]==results[0]["tool_call_id"]
        assert done["work_steps"][0]["result"]["verification"]=="core_connection_observation"
    asyncio.run(run())


def test_auxiliary_call_consumes_daily_limit_without_releasing_known_cost(world,monkeypatch):
    provider=CompactProvider()
    chat=app_for(world,monkeypatch,provider,daily_calls=5,chat_input_bytes=16384).state.chat
    async def run():
        conv,_=await seed(chat,world)
        turn,_=await send(chat,world,conv["conversation_id"],allow_compaction=True)
        await chat.process_one()
        assert len(await rows(world))==5
        assert not await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="failed" and done["error_code"]=="BUDGET_EXCEEDED"
        assert done["cost_status"]=="calculated" and done["cost_microunits"]>0
        assert len(provider.requests)==5
    asyncio.run(run())


def test_old_checkpoint_does_not_cross_new_private_history_boundary(world,monkeypatch):
    provider=CompactProvider()
    chat=app_for(world,monkeypatch,provider).state.chat
    async def run():
        conv,ids=await seed(chat,world,2)
        await send(chat,world,conv["conversation_id"],text="/compact",allow_compaction=True,compact_only=True)
        await chat.process_one()
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE chat_turns SET memory_refs_json='[{}]' WHERE turn_id=?",(ids[-1],))
        follow,_=await send(chat,world,conv["conversation_id"],text="只使用当前信息",allow_compaction=True)
        detail=await chat.get(world.owner,follow["turn_id"])
        assert detail["context_messages"]==[{"role":"user","content":"只使用当前信息"}]
    asyncio.run(run())


def test_restart_between_summary_and_reply_preserves_checkpoint_without_paid_wakeup(world,monkeypatch):
    provider=CompactProvider()
    chat=app_for(world,monkeypatch,provider,chat_input_bytes=16384).state.chat
    async def run():
        conv,_=await seed(chat,world)
        turn,_=await send(chat,world,conv["conversation_id"],allow_compaction=True)
        await chat.process_one()
        before=len(provider.requests)
        await chat.recover()
        assert not await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="interrupted" and done["cost_status"]=="calculated"
        assert len(provider.requests)==before
        follow,_=await send(chat,world,conv["conversation_id"],allow_compaction=True)
        assert "<compacted-summary>" in (await chat.get(world.owner,follow["turn_id"]))["context_messages"][0]["content"]
    asyncio.run(run())
