"""An approved result gets one bounded final exchange, never another execution."""

import asyncio
import json
import sqlite3
from datetime import timedelta
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from nexus.contracts.chat import ChatResultConsent, ChatResultPreview, ConversationUpdate
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict
from test_chat import FakeChat, rows
from test_chat_actions import SYSTEM, DIRECTORY, decision, setup_chat, submit
from test_harness import result_for


async def executed(chat, world, provider, *, directory=False, result=None):
    provider.reply = replace(provider.reply, native_tool_call={"id": "actual-native-call", "type": "function", "function": {
        "name": provider.reply.proposal["intent"], "arguments": json.dumps(provider.reply.proposal["arguments"])}})
    conv, turn = await submit(chat, world)
    await chat.process_one()
    turn = await chat.get(world.owner, turn["turn_id"])
    task = (await chat.decide_action(world.owner, UUID(turn["turn_id"]), decision(turn)))["task"]
    scope = chat.harness.scope(world.node_id)
    if directory:
        detail = await chat.harness.executions.detail(task.task_id, world.owner_id)
        await world.security.decide(UUID(detail["approvals"][0]["approval_id"]), world.owner, "approve", scope)
    command = await chat.harness.executions.dispatch(task.task_id, world.node, scope, uuid4(), uuid4(), 10)
    evidence = result_for(command, **({"result": result} if result else {}))
    await chat.harness.executions.store_result(world.node, evidence)
    await chat.harness.executions.verify(command.attempt_id)
    assert (await world.tasks.get(task.task_id, world.owner_id)).status == "succeeded"
    return conv, turn, task


def consent(value, **changes):
    return ChatResultConsent(**{**{key: value[key] for key in ("result_digest", "expected_revision")}, "allow_model": True, **changes})


@pytest.mark.parametrize("directory", [False, True])
def test_verified_preview_selection_native_result_final_and_followup(world, monkeypatch, directory):
    chat, provider = setup_chat(world, monkeypatch, DIRECTORY if directory else SYSTEM)
    async def run():
        conv, turn, task = await executed(chat, world, provider, directory=directory)
        assert not await chat.process_one() and len(provider.calls) == 1
        fields = ["entries"] if directory else ["os_name", "os_version"]
        value = await chat.preview_result(world.owner, UUID(turn["turn_id"]), ChatResultPreview(fields=fields))
        assert value["fields"] == sorted(fields) and set(value["data"]["result"]) == set(fields)
        assert value["data"]["verification"] == "node_report_schema_checked"
        assert len(await rows(world)) == 1
        with pytest.raises(AccessDenied):
            await chat.consent_result(world.owner, UUID(turn["turn_id"]), consent(value, allow_model=False))
        queued = await chat.consent_result(world.owner, UUID(turn["turn_id"]), consent(value))
        assert await chat.consent_result(world.owner, UUID(turn["turn_id"]), consent(value)) == queued
        final = FakeChat(text="根据已批准的结果，这是最终回答。")
        chat.provider = final
        await chat.process_one()
        result = await chat.get(world.owner, turn["turn_id"])
        assert result["phase"] == "summary" and result["status"] == "completed" and result["response_text"] == final.text
        assert result["initial_response_text"] == turn["response_text"]
        messages = final.requests[0]
        assert [m["role"] for m in messages] == ["user", "assistant", "tool"]
        assert messages[-2]["tool_calls"][0]["id"] == messages[-1]["tool_call_id"] == "actual-native-call"
        assert json.loads(messages[-1]["content"]) == value["data"]
        assert str(world.node_id) not in json.dumps(messages)
        calls = await rows(world)
        assert [c["chat_stage"] for c in calls] == [0, 1]
        assert calls[1]["reserved_microunits"] == 50000-calls[0]["cost_microunits"]
        assert result["model_call_count"] == 2 and result["cost_microunits"] == sum(c["cost_microunits"] for c in calls)
        assert len(await rows(world, "task_attempts")) == 1
        await chat.consent_result(world.owner, UUID(turn["turn_id"]), consent(value)); assert not await chat.process_one()
        assert len((await chat.history(world.owner, conv["conversation_id"]))["turns"]) == 1
        await submit(chat, world, conversation=conv, text="继续解释", allow_tools=False, node_id=None)
        await chat.process_one()
        assert final.requests[-1][1] == {"role": "assistant", "content": final.text}
        assert len(await rows(world, "tasks")) == 1
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["digest", "fields", "policy", "scope", "new_turn", "archive", "expired", "node", "source", "unknown", "failed", "schema", "old_call"])
def test_stale_unverified_or_unapproved_results_never_leave_core(world, monkeypatch, mode):
    chat, provider = setup_chat(world, monkeypatch, SYSTEM)
    async def run():
        conv, turn, task = await executed(chat, world, provider)
        value = await chat.preview_result(world.owner, UUID(turn["turn_id"]), ChatResultPreview())
        principal = world.owner
        if mode == "digest": value["result_digest"] = "0"*64
        elif mode == "fields":
            await chat.preview_result(world.owner, UUID(turn["turn_id"]), ChatResultPreview(fields=["os_name"]))
        elif mode == "policy": await world.security.set_grant(world.node_id, "system.info", enabled=False)
        elif mode == "scope": chat.harness.link.config.nodes.clear()
        elif mode == "new_turn": await submit(chat, world, conversation=conv)
        elif mode == "archive": await chat.update(world.owner, conv["conversation_id"], ConversationUpdate(archived=True))
        elif mode == "node": principal = world.node
        else:
            async with world.db.transaction() as conn:
                if mode == "expired":
                    stored = json.loads((await one(conn,"SELECT result_preview_json FROM chat_turns"))[0]); stored["expires_at"] = timestamp(utc_now())
                    await conn.execute("UPDATE chat_turns SET result_preview_json=?", (json.dumps(stored),))
                elif mode == "source": await conn.execute("UPDATE tasks SET revision=revision+1")
                elif mode == "unknown": await conn.execute("UPDATE tasks SET execution_outcome='unknown'")
                elif mode == "failed": await conn.execute("UPDATE tasks SET status='failed'")
                elif mode == "schema": await conn.execute("UPDATE tasks SET verification=NULL")
                elif mode == "old_call": await conn.execute("UPDATE chat_turns SET native_tool_call_json=NULL")
        with pytest.raises((AccessDenied, Conflict)):
            await chat.consent_result(principal, UUID(turn["turn_id"]), consent(value))
        assert len(provider.calls) == len(await rows(world)) == 1
    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["task_budget", "daily_cost", "daily_calls", "unknown_first", "price", "pause", "commit", "policy"])
def test_summary_rechecks_budget_control_policy_and_commit_before_http(world, monkeypatch, boundary):
    chat, provider = setup_chat(world, monkeypatch, SYSTEM)
    async def run():
        _, turn, _ = await executed(chat, world, provider)
        value = await chat.preview_result(world.owner, UUID(turn["turn_id"]), ChatResultPreview())
        await chat.consent_result(world.owner, UUID(turn["turn_id"]), consent(value))
        if boundary in {"task_budget", "unknown_first"}:
            async with world.db.transaction() as conn:
                await conn.execute("UPDATE model_calls SET cost_microunits=49999" if boundary == "task_budget" else "UPDATE model_calls SET cost_status='unknown'")
        elif boundary == "daily_cost": chat.config = chat.config.model_copy(update={"daily_cost_microunits": 49999})
        elif boundary == "daily_calls": chat.config = chat.config.model_copy(update={"daily_calls": 1})
        elif boundary == "price": chat.config = chat.config.model_copy(update={"price_valid_until": utc_now().date()-timedelta(days=1)})
        elif boundary == "pause": await chat.harness.control(world.owner, "pause", uuid4())
        elif boundary == "policy": await world.security.set_grant(world.node_id, "system.info", enabled=False)
        else:
            async with world.db.transaction() as conn:
                await conn.execute("CREATE TRIGGER reject_reservation BEFORE INSERT ON model_calls WHEN NEW.chat_stage=1 BEGIN SELECT RAISE(ABORT,'controlled failure'); END")
        if boundary == "commit":
            with pytest.raises(sqlite3.DatabaseError): await chat.process_one()
        else: await chat.process_one()
        assert len(provider.calls) == len(await rows(world)) == 1
        assert len(await rows(world, "task_attempts")) == 1
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["stop", "global", "shutdown", "recovery", "provider_failure"])
def test_final_exchange_stop_recovery_and_unknown_cost_do_not_repeat_tools(world, monkeypatch, mode):
    chat, provider = setup_chat(world, monkeypatch, SYSTEM)
    async def run():
        _, turn, _ = await executed(chat, world, provider)
        value = await chat.preview_result(world.owner, UUID(turn["turn_id"]), ChatResultPreview())
        await chat.consent_result(world.owner, UUID(turn["turn_id"]), consent(value))
        final = FakeChat(block=mode != "provider_failure", fail=mode == "provider_failure")
        chat.provider = final
        if mode == "recovery":
            row, call_id, task, partial = await chat.claim()
            await final.started.wait(); task.cancel(); await asyncio.gather(task, return_exceptions=True)
            chat.active = None
            await chat.recover()
        else:
            worker = asyncio.create_task(chat.process_one()); await final.started.wait()
            if mode == "stop": await chat.cancel(world.owner, turn["turn_id"])
            elif mode == "global": await chat.harness.control(world.owner, "cancel_all", uuid4())
            elif mode == "shutdown": worker.cancel()
            await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 3)
        result = await chat.get(world.owner, turn["turn_id"])
        assert result["status"] in {"stopped", "interrupted", "failed"}
        assert result["cost_status"] == "unknown" and result["initial_response_text"]
        calls = await rows(world)
        assert calls[1]["reserved_microunits"]+calls[0]["cost_microunits"] == 50000
        assert len(calls) == 2 and len(await rows(world, "task_attempts")) == 1 and final.closed
        assert (await rows(world, "tasks"))[0]["status"] == "succeeded"
        await chat.consent_result(world.owner, UUID(turn["turn_id"]), consent(value))
        assert not await chat.process_one() and len(final.requests) == 1
    asyncio.run(run())


def test_summary_provider_disallows_further_actions(world, monkeypatch):
    chat, provider = setup_chat(world, monkeypatch, SYSTEM)
    async def run():
        _, turn, _ = await executed(chat, world, provider)
        value = await chat.preview_result(world.owner, UUID(turn["turn_id"]), ChatResultPreview())
        await chat.consent_result(world.owner, UUID(turn["turn_id"]), consent(value))
        await chat.process_one()  # The deliberately hostile provider proposes a second tool.
        assert provider.calls[1][1] == {"generate_title": False, "allow_tools": False, "final_result": True}
        assert (await chat.get(world.owner, turn["turn_id"]))["status"] == "failed"
        assert len(await rows(world, "tasks")) == 1
    asyncio.run(run())


def test_large_directory_preview_is_bounded_and_explicitly_truncated(world, monkeypatch):
    chat, provider = setup_chat(world, monkeypatch, {"intent":"list_directory", "arguments":{"root_id":"workspace","limit":200}})
    async def run():
        entries = [{"name":str(i)+"x"*200,"kind":"file"} for i in range(70)]
        entries[0]["name"] = "<img onerror=alert(1)>"
        _, turn, _ = await executed(chat, world, provider, directory=True, result={"root_id":"workspace","entries":entries,"truncated":False})
        value = await chat.preview_result(world.owner, UUID(turn["turn_id"]), ChatResultPreview())
        assert value["data"]["truncated"] and value["data"]["result"]["truncated"]
        assert 0 < len(value["data"]["result"]["entries"]) < len(entries)
        assert len(json.dumps(value["data"]["result"],separators=(',',':')).encode()) <= 8192
        with pytest.raises(AccessDenied):
            await chat.preview_result(world.owner, UUID(turn["turn_id"]), ChatResultPreview(fields=["os_name"]))
    asyncio.run(run())
