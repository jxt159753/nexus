"""Failed web reads can continue; terminal work gets one authorized LLM reply."""

import asyncio
import json
from dataclasses import replace

import httpx2
import pytest

from nexus.core.models.budget import accounted_cost
from nexus.core.models.chat import build_request
from nexus.core.models.deepseek import ProviderFailure
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, utc_now
from test_chat import rows
from test_chat_skills import app_for
from test_chat_work import basic, WorkProvider
from test_web_work import configured, drain
from test_workspace_work import send_workspace


SEARCH = {"intent": "web_search", "arguments": {"query": "泰安一中 校园 图片"}}
FETCH = {"intent": "web_fetch", "arguments": {"url": "https://example.com/campus"}}
OTHER = {"intent": "web_fetch", "arguments": {"url": "https://example.org/campus"}}
SOURCE = {"url": FETCH["arguments"]["url"], "title": "校园图片", "snippet": "校园来源摘要"}


@pytest.mark.parametrize("cost_limits", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "disconnect", "invalid"])
def test_failed_web_read_continues_to_alternative_and_llm_without_losing_cost(world, monkeypatch, cost_limits, failure):
    app = app_for(world, monkeypatch)
    seen = []

    async def handle(request):
        value = json.loads(request.content)
        seen.append(value)
        if request.url.path.endswith("search"):
            return httpx2.Response(200, json={"results": [SOURCE]})
        if value["url"] == FETCH["arguments"]["url"]:
            if failure == "timeout":
                raise TimeoutError()
            if failure == "disconnect":
                raise httpx2.ReadError("synthetic disconnected read")
            return httpx2.Response(200, content=b"invalid")
        return httpx2.Response(200, json={"title": "另一个来源", "content": "校园资料"})

    async def run():
        await basic(app, world)
        chat = configured(app, handle)
        chat.config.cost_limits_enabled = cost_limits
        turn = await send_workspace(app, world, [SEARCH, FETCH, OTHER])
        done = await drain(chat, world, turn["turn_id"])
        assert done["status"] == "completed" and done["cost_status"] == "unknown"
        assert [s["status"] for s in done["work_steps"]] == ["succeeded", "failed", "succeeded"]
        failed = done["work_steps"][1]["result"]
        assert failed["verification"] is None and failed["cost_microunits"] is None
        assert failed["cost_status"] == "unknown" and "message" in failed
        assert len(seen) == 3 and done["model_call_count"] == 4
        evidence = [json.loads(m["content"]) for m in chat.provider.calls[-1][0] if m["role"] == "tool"]
        assert evidence[1] == failed and evidence[0]["data"]["sources"] == [SOURCE]
        calls = await rows(world)
        unknown = next(c for c in calls if c["cost_status"] == "unknown")
        assert unknown["outcome"] == "completed" and unknown["reserved_microunits"] == 750
        assert accounted_cost(calls) == sum(c["cost_microunits"] or 0 for c in calls) + 750
        assert done["presentation"]["final_text"] == done["response_text"]
        assert "部分操作未完成" in done["presentation"]["label"]
        assert done["presentation"]["collapse_process"]
        assert done["presentation"]["final_text"] and done["work_steps"]

    asyncio.run(run())


def test_unknown_web_billing_does_not_allow_same_request_to_be_sent_again(world, monkeypatch):
    app = app_for(world, monkeypatch)
    seen = []

    async def handle(request):
        seen.append(request)
        raise TimeoutError()

    async def run():
        await basic(app, world)
        chat = configured(app, handle)
        turn = await send_workspace(app, world, [FETCH, FETCH])
        done = await drain(chat, world, turn["turn_id"])
        assert len(seen) == 1 and done["web_call_count"] == 1
        result = done["work_steps"][1]["result"]
        assert result["error_code"] == "WEB_ALREADY_ATTEMPTED"
        assert result["execution_outcome"] == "not_started"
        assert done["status"] == "completed" and done["model_call_count"] == 3

    asyncio.run(run())


def test_batch_continues_after_timeout_and_returns_every_native_call(world, monkeypatch):
    app = app_for(world, monkeypatch)

    async def handle(request):
        if json.loads(request.content)["url"] == FETCH["arguments"]["url"]:
            raise TimeoutError()
        return httpx2.Response(200, json={"title": "校园", "content": "另一个页面可读"})

    async def run():
        await basic(app, world)
        chat = configured(app, handle)
        turn = await send_workspace(app, world, [])
        inner = WorkProvider([FETCH, OTHER])

        class Batch:
            async def invoke(self, messages, receive, **options):
                first = await inner.invoke(messages, receive, **options)
                if first.proposal:
                    second = await inner.invoke(messages, receive, **options)
                    return replace(first, work_calls=((first.proposal, first.native_tool_call), (second.proposal, second.native_tool_call)))
                return first

        chat.provider = Batch()
        done = await drain(chat, world, turn["turn_id"])
        assert [s["status"] for s in done["work_steps"]] == ["failed", "succeeded"]
        assert done["model_call_count"] == 2
        results = [m for m in inner.calls[-1][0] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in results] == ["work-call-1", "work-call-2"]

    asyncio.run(run())


@pytest.mark.parametrize("followup", ["answer", "model_failure", "tools", "rejected_tools", "cancel", "revoke", "pause", "restart", "daily_limit"])
def test_terminal_tool_error_has_one_tool_free_llm_reply_or_readable_fallback(world, monkeypatch, followup):
    app = app_for(world, monkeypatch)

    async def run():
        await basic(app, world)
        chat = app.state.chat
        turn = await send_workspace(app, world, [{"intent": "get_node_status", "arguments": {}}])
        await chat.process_one()
        calls = []

        class Explain:
            async def invoke(self, messages, receive, **options):
                calls.append((messages, options))
                assert options["allow_tools"] is False and options["work_catalog"] == []
                assert options["final_result"] is True and options["generate_title"] is False
                body = build_request(chat.config, messages, **options)
                assert "tools" not in body
                assert "Do not output only error codes" in body["messages"][0]["content"]
                assert "WORK_ACTION_UNAVAILABLE" in messages[-1]["content"]
                if followup == "model_failure":
                    raise ProviderFailure("MODEL_TIMEOUT")
                inner = WorkProvider([FETCH] if followup == "tools" else [])
                reply = replace(await inner.invoke(messages, receive, **options), content="这次工具暂时不可用，没有完成设备查询。请检查设备连接后再试。")
                if followup == "rejected_tools":
                    return replace(reply, rejected_calls=({"id": "unexpected", "type": "function", "function": {"name": "web_fetch", "arguments": "{}"}},),
                                   validation_errors=({"index": 0, "type": "missing"},))
                return reply

        chat.provider = Explain()
        async with world.db.transaction() as conn:
            await chat.work.abort(conn, turn["turn_id"], "WORK_ACTION_UNAVAILABLE", utc_now())
        if followup == "cancel":
            await chat.cancel(world.owner, turn["turn_id"])
        elif followup == "revoke":
            await chat.work.revoke(world.owner, turn["conversation_id"])
        elif followup == "pause":
            async with world.db.transaction() as conn:
                await conn.execute("UPDATE system_state SET paused=1")
        elif followup == "restart":
            await chat.recover()
        elif followup == "daily_limit":
            chat.config.daily_calls = 1
        for _ in range(4):
            await chat.process_one()
        done = await chat.get(world.owner, turn["turn_id"])
        expected = 1 if followup in {"answer", "model_failure", "tools", "rejected_tools"} else 0
        assert len(calls) == expected and len(await rows(world)) == 1 + expected
        assert len(done["work_steps"]) == 1 and done["work_steps"][0]["status"] == "stopped"
        assert done["presentation"]["final_text"]
        if followup == "answer":
            assert done["presentation"]["final_text"].startswith("这次工具暂时不可用")
            assert done["error_code"] == "WORK_ACTION_UNAVAILABLE"
        await chat.history(world.owner, turn["conversation_id"])
        assert len(calls) == expected

    asyncio.run(run())


def test_fallback_keeps_source_links_when_daily_calls_prevent_llm(world, monkeypatch):
    app = app_for(world, monkeypatch)

    async def handle(request):
        if request.url.path.endswith("search"):
            return httpx2.Response(200, json={"results": [SOURCE]})
        raise TimeoutError()

    async def run():
        await basic(app, world)
        chat = configured(app, handle)
        chat.config.daily_calls = 4  # two model calls, search, then failed fetch
        turn = await send_workspace(app, world, [SEARCH, FETCH])
        done = await drain(chat, world, turn["turn_id"])
        assert done["error_code"] == "BUDGET_EXCEEDED"
        text = done["presentation"]["final_text"]
        assert "校园图片" in text and SOURCE["url"] in text
        assert "摘要不代表已读取" in text and "BUDGET_" not in text
        assert len(await rows(world)) == 4

    asyncio.run(run())


def test_unknown_model_or_unreconciled_web_still_blocks_new_calls():
    for provider, outcome in [("deepseek", "completed"), ("deepseek-web", "unknown"), ("deepseek-web", "started")]:
        with pytest.raises(AccessDenied):
            accounted_cost([{"provider": provider, "outcome": outcome, "cost_status": "unknown",
                             "web_step_id": "step", "finished_at": "now", "reserved_microunits": 750, "cost_microunits": None}])


def test_pausing_goal_does_not_start_failure_explanation(world, monkeypatch):
    from test_plugin_autonomy_recovery import control, goal_ready
    app = app_for(world, monkeypatch)

    async def run():
        chat = app.state.chat
        conversation, goal, turn = await goal_ready(app, world)
        count = len(await rows(world))
        await control(chat, world.owner, conversation["conversation_id"], goal["goal_id"], "paused")
        assert not await chat.process_one()
        assert len(await rows(world)) == count
        done = await chat.get(world.owner, turn["turn_id"])
        assert done["status"] == "stopped"
        assert "已暂停" in done["presentation"]["final_text"]

    asyncio.run(run())
