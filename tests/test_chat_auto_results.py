"""Per-action result consent closes the round without a second UI approval."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from nexus.contracts.chat import ChatActionDecision, ConversationUpdate
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict
from test_chat import FakeChat, rows
from test_chat_actions import SYSTEM, DIRECTORY, decision, setup_chat, submit
from test_chat_results import executed


@pytest.mark.parametrize("directory", [False, True])
def test_automatic_final_answer_uses_same_round_then_followup_sees_real_answer(world, monkeypatch, directory):
    chat, provider = setup_chat(world, monkeypatch, DIRECTORY if directory else SYSTEM)
    async def run():
        conv, turn, task = await executed(chat, world, provider, directory=directory, automatic=True)
        pending = await chat.get(world.owner, turn["turn_id"])
        assert pending["auto_result_allowed"] and pending["auto_result_status"] == "waiting"
        assert (await chat.history(world.owner, conv["conversation_id"]))["active_turn_id"] == turn["turn_id"]
        with pytest.raises(Conflict, match="BUSY"):
            await submit(chat, world, conversation=conv, text="信息是啥")
        again = decision(turn).model_copy(update={"allow_result_model": True})
        assert (await chat.decide_action(world.owner, UUID(turn["turn_id"]), again))["task"].task_id == task.task_id
        with pytest.raises(Conflict):
            await chat.decide_action(world.owner, UUID(turn["turn_id"]), decision(turn))
        final = FakeChat(text="已读取到 Windows 系统信息。")
        chat.provider = final
        await chat.process_one()
        result = await chat.get(world.owner, turn["turn_id"])
        assert result["phase"] == "summary" and result["status"] == "completed" and result["response_text"] == final.text
        assert result["auto_result_status"] == "queued" and result["model_call_count"] == 2
        assert json.loads(final.requests[0][-1]["content"])["result"] == (await world.tasks.get(task.task_id, world.owner_id)).result.model_dump(mode="json")
        calls = await rows(world)
        assert calls[1]["reserved_microunits"] + calls[0]["cost_microunits"] == 50000
        await chat.process_one(); assert len(await rows(world)) == 2
        await submit(chat, world, conversation=conv, text="信息是啥", allow_tools=False, node_id=None)
        await chat.process_one()
        assert final.requests[-1][1]["content"] == final.text
        assert len(await rows(world, "tasks")) == len(await rows(world, "task_attempts")) == 1
    asyncio.run(run())


def test_unchecked_action_never_automatically_shares_results(world, monkeypatch):
    chat, provider = setup_chat(world, monkeypatch, SYSTEM)
    async def run():
        conv, turn, _ = await executed(chat, world, provider)
        assert not await chat.process_one() and len(await rows(world)) == 1
        row = await chat.get(world.owner, turn["turn_id"])
        assert not row["auto_result_allowed"] and row["auto_result_status"] == "none"
        assert row["summary_consented_at"] is None and row["result_digest"] is None
        assert (await chat.history(world.owner, conv["conversation_id"]))["active_turn_id"] is None
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["stop", "global", "archive", "expired", "policy", "scope", "unknown", "failed", "schema", "changed_action"])
def test_revoked_or_unverified_automatic_results_make_no_second_call(world, monkeypatch, mode):
    chat, provider = setup_chat(world, monkeypatch, SYSTEM)
    async def run():
        conv, turn, _ = await executed(chat, world, provider, automatic=True)
        if mode == "stop": await chat.cancel(world.owner, turn["turn_id"])
        elif mode == "global": await chat.harness.control(world.owner, "cancel_all", uuid4())
        elif mode == "archive": await chat.update(world.owner, conv["conversation_id"], ConversationUpdate(archived=True))
        elif mode == "policy": await world.security.set_grant(world.node_id, "system.info", enabled=False)
        elif mode == "scope": chat.harness.link.config.nodes.clear()
        else:
            async with world.db.transaction() as conn:
                if mode == "expired":
                    value = json.loads((await one(conn, "SELECT auto_result_json FROM chat_turns"))[0]); value["expires_at"] = timestamp(utc_now())
                    await conn.execute("UPDATE chat_turns SET auto_result_json=?", (json.dumps(value),))
                elif mode == "unknown": await conn.execute("UPDATE tasks SET execution_outcome='unknown'")
                elif mode == "failed": await conn.execute("UPDATE tasks SET status='failed'")
                elif mode == "schema": await conn.execute("UPDATE tasks SET verification=NULL")
                elif mode == "changed_action": await conn.execute("UPDATE chat_turns SET proposal_digest=?", ("0"*64,))
        assert not await chat.process_one()
        assert len(provider.calls) == len(await rows(world)) == 1
        result = await chat.get(world.owner, turn["turn_id"])
        assert result["auto_result_status"] == ("stopped" if mode in {"stop", "global"} else "unavailable")
        assert (await chat.history(world.owner, conv["conversation_id"]))["active_turn_id"] is None
        await chat.recover(); assert not await chat.process_one() and len(provider.calls) == 1
    asyncio.run(run())


def test_pause_blocks_automatic_exchange_then_resume_continues_once(world, monkeypatch):
    chat, provider = setup_chat(world, monkeypatch, SYSTEM)
    async def run():
        _, turn, _ = await executed(chat, world, provider, automatic=True)
        await chat.harness.control(world.owner, "pause", uuid4())
        assert not await chat.process_one() and len(await rows(world)) == 1
        assert (await chat.get(world.owner, turn["turn_id"]))["auto_result_status"] == "waiting"
        final = FakeChat(); chat.provider = final
        await chat.harness.control(world.owner, "resume", uuid4()); await chat.process_one()
        assert len(await rows(world)) == 2 and len(final.requests) == 1
    asyncio.run(run())


@pytest.mark.parametrize("directory", [False, True])
def test_waiting_for_device_or_directory_approval_makes_no_result_call_and_can_stop(world, monkeypatch, directory):
    chat, provider = setup_chat(world, monkeypatch, DIRECTORY if directory else SYSTEM)
    async def run():
        provider.reply = replace(provider.reply, native_tool_call={"id":"native-one","type":"function","function":{
            "name":provider.reply.proposal["intent"],"arguments":json.dumps(provider.reply.proposal["arguments"])}})
        _, turn = await submit(chat, world); await chat.process_one()
        turn = await chat.get(world.owner, turn["turn_id"])
        task = (await chat.decide_action(world.owner, UUID(turn["turn_id"]), decision(turn).model_copy(update={"allow_result_model":True})))["task"]
        assert task.status == ("awaiting_approval" if directory else "ready")
        assert not await chat.process_one() and len(await rows(world)) == 1
        await chat.cancel(world.owner, turn["turn_id"])
        assert (await world.tasks.get(task.task_id, world.owner_id)).status == task.status
        assert not await chat.process_one() and len(await rows(world)) == 1
    asyncio.run(run())


def test_recovery_of_waiting_result_does_not_repeat_tool_or_first_model_call(world, monkeypatch):
    chat, provider = setup_chat(world, monkeypatch, SYSTEM)
    async def run():
        _, turn, _ = await executed(chat, world, provider, automatic=True)
        await chat.recover()
        final = FakeChat(); chat.provider = final
        await chat.process_one(); await chat.recover(); await chat.process_one()
        assert (await chat.get(world.owner, turn["turn_id"]))["status"] == "completed"
        assert len(await rows(world)) == 2 and len(final.requests) == 1
        assert len(await rows(world, "task_attempts")) == 1
    asyncio.run(run())


def test_result_consent_is_strict_and_requires_confirmation():
    for value in ({"decision":"reject","allow_result_model":True}, {"decision":"confirm","allow_result_model":"yes"}):
        with pytest.raises(ValidationError): ChatActionDecision(proposal_digest="0"*64, **value)


def test_result_queue_write_failure_cannot_start_second_model_exchange(world, monkeypatch):
    chat, provider = setup_chat(world, monkeypatch, SYSTEM)
    async def run():
        _, turn, _ = await executed(chat, world, provider, automatic=True)
        async with world.db.transaction() as conn:
            await conn.execute("CREATE TRIGGER fail_auto_queue BEFORE UPDATE OF phase ON chat_turns WHEN NEW.phase='summary' BEGIN SELECT RAISE(ABORT,'controlled queue failure'); END")
        with pytest.raises(sqlite3.DatabaseError): await chat.process_one()
        assert len(await rows(world)) == len(provider.calls) == 1
        assert (await chat.get(world.owner, turn["turn_id"]))["auto_result_status"] == "waiting"
        async with world.db.transaction() as conn: await conn.execute("DROP TRIGGER fail_auto_queue")
        chat.provider = FakeChat(); await chat.process_one()
        assert len(await rows(world)) == 2
    asyncio.run(run())


def test_followup_cannot_interleave_confirmation_and_task_link(world, monkeypatch):
    chat, provider = setup_chat(world, monkeypatch, SYSTEM)
    async def run():
        provider.reply=replace(provider.reply,native_tool_call={"id":"native-race","type":"function","function":{"name":"get_system_info","arguments":"{}"}})
        conv,turn=await submit(chat,world);await chat.process_one();turn=await chat.get(world.owner,turn['turn_id'])
        started,release=asyncio.Event(),asyncio.Event();original=chat.harness.submit
        async def delayed(*args,**kwargs):
            started.set();await release.wait();return await original(*args,**kwargs)
        monkeypatch.setattr(chat.harness,'submit',delayed)
        confirming=asyncio.create_task(chat.decide_action(world.owner,UUID(turn['turn_id']),decision(turn).model_copy(update={'allow_result_model':True})))
        await started.wait()
        following=asyncio.create_task(submit(chat,world,conversation=conv,text='信息是啥'))
        await asyncio.sleep(.05);assert not following.done()
        release.set();await confirming
        with pytest.raises(Conflict,match='BUSY'):await following
        assert len(await rows(world,'chat_turns'))==len(await rows(world,'tasks'))==1
    asyncio.run(run())


def test_v9_migration_preserves_both_model_stages_and_never_grants_old_consent(tmp_path):
    import hashlib
    from contextlib import closing
    from importlib.resources import files
    from nexus.core.storage.database import Database
    path=tmp_path/'core.sqlite3';now=timestamp(utc_now());owner,cid,tid=[str(uuid4()) for _ in range(3)]
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)')
        for resource in sorted(files('nexus.core.storage').joinpath('migrations').iterdir(),key=lambda p:p.name):
            if resource.name.endswith('.sql') and int(resource.name[:3])<=9:
                source=resource.read_text(encoding='utf-8');conn.executescript(source)
                conn.execute('INSERT INTO schema_migrations VALUES(?,?,?)',(int(resource.name[:3]),hashlib.sha256(source.encode()).hexdigest(),now));conn.commit()
        conn.execute("INSERT INTO identities VALUES(?,'owner','synthetic-hash',NULL,?)",(owner,now))
        conn.execute("INSERT INTO conversations(conversation_id,owner_id,request_id,title,created_at) VALUES(?,?,?,'旧标题',?)",(cid,owner,str(uuid4()),now))
        conn.execute("INSERT INTO chat_turns(turn_id,conversation_id,owner_id,request_id,request_json,text,context_json,context_digest,context_count,context_truncated,response_text,status,phase,created_at,expires_at) VALUES(?,?,?,?,?,'旧问题','[]','hash',0,0,'旧总结','completed','summary',?,?)",(tid,cid,owner,str(uuid4()),json.dumps({'include_context':False}),now,now))
        for stage in (0,1):conn.execute("INSERT INTO model_calls(call_id,provider,model,cost_microunits,cost_status,created_at,chat_turn_id,chat_stage,reserved_microunits) VALUES(?,'deepseek','historical',?,'calculated',?,?,?,?)",(str(uuid4()),10+stage,now,tid,stage,50000-stage*10))
        conn.commit();saved={table:conn.execute('SELECT * FROM '+table).fetchall() for table in ('conversations','chat_turns','model_calls')}
    asyncio.run(Database(path).initialize())
    with closing(sqlite3.connect(path)) as conn:
        for table,rows_before in saved.items():assert [r[:len(rows_before[0])] for r in conn.execute('SELECT * FROM '+table).fetchall()]==rows_before
        assert conn.execute('SELECT auto_result_json,auto_result_status FROM chat_turns').fetchone()==(None,'none')
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
    backups=list(tmp_path.glob('core.sqlite3.before-v10-*.sqlite3'));assert len(backups)==1
    with closing(sqlite3.connect(backups[0])) as conn:assert conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
