"""Public progress survives refresh; only a settled final reply may collapse it."""

import asyncio
import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from nexus.contracts.chat import ChatResultPreview
from nexus.core.security.types import AccessDenied
from nexus.core.storage.execution import ExecutionRepository
from test_chat import FakeChat, app_for as plain_app, conversation, send
from test_chat_actions import SYSTEM, setup_chat
from test_chat_results import consent, executed
from test_chat_skills import app_for
from test_chat_work import WorkProvider, basic, setup


class ProgressProvider(WorkProvider):
    async def invoke(self, messages, receive, **options):
        reply = await super().invoke(messages, receive, **options)
        if reply.proposal:
            return replace(reply, content="先查询设备连接状态，再汇总实际结果。")
        return reply


def test_progress_is_retained_and_final_reply_collapses_without_extra_call(world, monkeypatch):
    app = app_for(world, monkeypatch)

    async def run():
        chat = app.state.chat
        conv, _, turn = await setup(app, world, [{"intent": "get_node_status", "arguments": {}}])
        chat.provider = ProgressProvider([{"intent": "get_node_status", "arguments": {}}])
        assert turn["presentation"]["phase"] == "queued"
        await chat.process_one()
        pending = await chat.get(world.owner, turn["turn_id"])
        assert pending["status"] == "completed"  # Only this model exchange ended.
        assert pending["presentation"]["phase"] == "working"
        assert pending["presentation"]["final_text"] is None
        assert not pending["presentation"]["collapse_process"]
        assert pending["presentation"]["process"][0]["text"].startswith("先查询")
        await chat.process_one()
        done = await chat.get(world.owner, turn["turn_id"])
        display = done["presentation"]
        assert display["phase"] == "completed" and display["collapse_process"]
        assert display["final_text"] == done["response_text"] and not display["live_text"]
        assert display["process"] == pending["presentation"]["process"]
        assert len(done["work_steps"]) == 1 and done["model_call_count"] == 2
        history = await chat.history(world.owner, conv["conversation_id"])
        assert history["turns"][0]["presentation"] == display
        assert len(chat.provider.calls) == 2
        assert "request_body" not in json.dumps(display) and "tool_call" not in json.dumps(display)

    asyncio.run(run())


def test_streaming_final_stays_provisional_until_commit(world, monkeypatch):
    app = app_for(world, monkeypatch)

    async def run():
        chat = app.state.chat
        _, _, turn = await setup(app, world, [{"intent": "get_node_status", "arguments": {}}])
        await chat.process_one()
        final = FakeChat(block=True, text="总结：设备离线，未执行设备操作。")
        chat.provider = final
        worker = asyncio.create_task(chat.process_one())
        await asyncio.wait_for(final.started.wait(), 3)
        live = (await chat.get(world.owner, turn["turn_id"]))["presentation"]
        assert live["phase"] == "responding" and live["label"] == "正在整理总结"
        assert live["live_text"] == final.text[:4]
        assert live["final_text"] is None and not live["collapse_process"]
        await chat.cancel(world.owner, turn["turn_id"])
        await asyncio.wait_for(worker, 3)
        stopped = (await chat.get(world.owner, turn["turn_id"]))["presentation"]
        assert stopped["phase"] == "stopped" and stopped["live_text"] == final.text[:4]
        assert "停止" in stopped["final_text"] and stopped["collapse_process"]
        assert final.closed

    asyncio.run(run())


@pytest.mark.parametrize("reason,fail,phase", [("stop", False, "completed"), ("length", False, "incomplete"), ("stop", True, "failed")])
def test_plain_reply_and_incomplete_or_failed_partial_are_distinct(world, monkeypatch, reason, fail, phase):
    provider = FakeChat(reason=reason, fail=fail)
    chat = plain_app(world, monkeypatch, provider).state.chat

    async def run():
        conv = await conversation(chat, world)
        turn, _ = await send(chat, world, conv["conversation_id"])
        await chat.process_one()
        display = (await chat.get(world.owner, turn["turn_id"]))["presentation"]
        assert display["phase"] == phase
        assert display["collapse_process"] == (phase != "completed")
        assert not display["process"]
        if phase == "completed":
            assert display["final_text"] == provider.text
        else:
            assert display["final_text"] and display["live_text"]
            assert "MODEL_" not in display["final_text"]

    asyncio.run(run())


def test_cancel_keeps_process_open_until_real_child_execution_stops(world, monkeypatch):
    app = app_for(world, monkeypatch)

    async def run():
        await basic(app, world)
        chat = app.state.chat
        _, _, turn = await setup(app, world, [{"intent": "read_file", "arguments": {"root_id": "workspace", "path": "note"}}])
        await chat.process_one()
        await chat.process_one()
        step = (await chat.get(world.owner, turn["turn_id"]))["work_steps"][0]
        repo = ExecutionRepository(world.db, world.security, 32768)
        command = await repo.dispatch(UUID(step["task_id"]), world.node, app.state.harness.scope(world.node_id), uuid4(), uuid4(), 10)
        await chat.cancel(world.owner, turn["turn_id"])
        stopping = (await chat.get(world.owner, turn["turn_id"]))["presentation"]
        assert stopping["phase"] == "stopping" and not stopping["collapse_process"]
        assert stopping["final_text"] is None
        await repo.cancelled(world.node, command.attempt_id)
        stopped = (await chat.get(world.owner, turn["turn_id"]))["presentation"]
        assert stopped["phase"] == "stopped" and stopped["collapse_process"]

    asyncio.run(run())


def test_recovery_is_visible_and_reading_history_does_not_resume_model(world, monkeypatch):
    app = app_for(world, monkeypatch)

    async def run():
        chat = app.state.chat
        conv, _, turn = await setup(app, world, [{"intent": "get_node_status", "arguments": {}}])
        await chat.process_one()
        await chat.recover()
        display = (await chat.get(world.owner, turn["turn_id"]))["presentation"]
        assert display["phase"] == "recovering" and not display["collapse_process"]
        await chat.history(world.owner, conv["conversation_id"])
        assert len(chat.provider.calls) == 1
        with pytest.raises(AccessDenied):
            await chat.get(world.node, turn["turn_id"])

    asyncio.run(run())


def test_legacy_manual_result_consent_is_preserved(world, monkeypatch):
    chat, provider = setup_chat(world, monkeypatch, SYSTEM)

    async def run():
        _, turn, _ = await executed(chat, world, provider)
        finished = await chat.get(world.owner, turn["turn_id"])
        assert finished["presentation"]["final_text"] is None
        assert not finished["presentation"]["collapse_process"]
        preview = await chat.preview_result(world.owner, UUID(turn["turn_id"]), ChatResultPreview())
        await chat.consent_result(world.owner, UUID(turn["turn_id"]), consent(preview))
        chat.provider = FakeChat(text="总结：系统信息已读取，结果通过原有结构检查。")
        await chat.process_one()
        done = await chat.get(world.owner, turn["turn_id"])
        assert done["presentation"]["final_text"] == done["response_text"]
        assert done["presentation"]["collapse_process"]
        assert done["presentation"]["process"][0]["text"] == turn["response_text"]

    asyncio.run(run())


def test_long_progress_is_bounded_but_full_journal_is_preserved(world, monkeypatch):
    app = app_for(world, monkeypatch)

    async def run():
        chat = app.state.chat
        _, _, turn = await setup(app, world, [{"intent": "get_node_status", "arguments": {}}])
        provider = chat.provider
        invoke = provider.invoke

        async def long_update(*args, **options):
            return replace(await invoke(*args, **options), content="公开进度" * 500)

        monkeypatch.setattr(provider, "invoke", long_update)
        await chat.process_one()
        display = (await chat.get(world.owner, turn["turn_id"]))["presentation"]
        assert len(display["process"][0]["text"]) == 1200 and display["process"][0]["truncated"]
        journal = await chat.journal.detail(world.owner, turn["turn_id"])
        assert len(json.loads(journal["exchanges"][0]["response_json"])["content"]) == 2000

    asyncio.run(run())
