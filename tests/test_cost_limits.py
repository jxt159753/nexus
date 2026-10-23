"""Owner-disabled USD limits keep bounded calls, receipts and cost evidence."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from nexus.core.cognitive.chat_compaction import remaining
from nexus.core.models.budget import budget_error
from nexus.core.models.chat import build_request
from nexus.core.models.config import ModelConfig
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import utc_now
from test_chat import app_for, conversation, rows, send


def test_disabled_amounts_do_not_shrink_output_or_audit_reservation():
    config = ModelConfig(enabled=True, chat_enabled=True, cost_limits_enabled=False,
                         task_cost_microunits=0, daily_cost_microunits=0)
    assert config.chat_output_limit(0) == 256_000
    assert config.chat_output_limit(0, compact=True) == 1024
    assert config.model_reservation() == 317_031
    assert build_request(config, [{"role": "user", "content": "Hello"}])["max_tokens"] == 256_000
    calls = [{"cost_status": "calculated", "outcome": "completed", "cost_microunits": 9_000_000}]
    assert remaining(config, calls) == config.model_reservation()
    calls[0]["cost_status"] = "unknown"
    with pytest.raises(AccessDenied):
        remaining(config, calls)


def test_chat_above_former_task_and_daily_caps_still_settles_and_continues(world, monkeypatch):
    from test_chat_work import setup
    app = app_for(world, monkeypatch, cost_limits_enabled=False, task_cost_microunits=0, daily_cost_microunits=0)
    chat = app.state.chat

    async def run():
        _, _, turn = await setup(app, world, [{"intent": "get_node_status", "arguments": {}}])
        inner = chat.provider

        class Expensive:
            async def invoke(self, messages, receive, **options):
                reply = await inner.invoke(messages, receive, **options)
                return replace(reply, output_tokens=60_000)

        chat.provider = Expensive()
        await chat.process_one()
        await chat.process_one()
        done = await chat.get(world.owner, turn["turn_id"])
        assert done["status"] == "completed" and done["model_call_count"] == 2
        calls = await rows(world)
        assert all(call["cost_status"] == "calculated" and call["cost_microunits"] > 50_000 for call in calls)
        assert all(call["reserved_microunits"] == 317_031 for call in calls)
        exchanges = (await chat.journal.detail(world.owner, turn["turn_id"]))["exchanges"]
        assert all(json.loads(exchange["request_body"])["max_tokens"] == 256_000 for exchange in exchanges)

    asyncio.run(run())


def test_disabled_usd_caps_preserve_call_count_price_and_model_guards(world, monkeypatch):
    chat = app_for(world, monkeypatch, cost_limits_enabled=False, daily_cost_microunits=0, daily_calls=1).state.chat

    async def run():
        conv = await conversation(chat, world)
        await send(chat, world, conv["conversation_id"])
        await chat.process_one()
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE model_calls SET cost_microunits=9000000,cost_status='unknown'")
            now = utc_now()
            assert await budget_error(conn, chat.config, now) == "BUDGET_EXCEEDED"
            config = chat.config.model_copy(update={"daily_calls": 2})
            assert await budget_error(conn, config, now) is None
            assert await budget_error(conn, config.model_copy(update={"enabled": False}), now) == "MODEL_DISABLED"
            assert await budget_error(conn, config.model_copy(update={"price_valid_until": now.date()-timedelta(days=1)}), now) == "MODEL_PRICING_EXPIRED"
        turn, _ = await send(chat, world, conv["conversation_id"])
        assert not await chat.process_one()
        assert (await chat.get(world.owner, turn["turn_id"]))["error_code"] == "BUDGET_EXCEEDED"
        assert len(await rows(world)) == 1

    asyncio.run(run())


@pytest.mark.parametrize("plugins", [False, True])
def test_web_charge_is_not_blocked_by_disabled_zero_amounts(world, monkeypatch, plugins):
    import httpx2
    from test_chat_work import basic
    from test_web_work import configured, drain
    from test_workspace_work import send_workspace
    app = app_for(world, monkeypatch, cost_limits_enabled=False, task_cost_microunits=0, daily_cost_microunits=0,
                  chat_max_output_tokens=32, input_price=1, output_price=1)
    app.state.chat.plugins.config.enabled = plugins

    async def handle(request):
        return httpx2.Response(200, json={"results": [{"url": "https://example.com/", "title": "Source", "snippet": "Evidence"}]})

    async def run():
        await basic(app, world)
        chat = configured(app, handle)
        turn = await send_workspace(app, world, [{"intent": "web_search", "arguments": {"query": "public sources"}}])
        done = await drain(chat, world, turn["turn_id"])
        assert done["status"] == "completed" and done["web_call_count"] == 1
        calls = await rows(world)
        web = next(call for call in calls if call["provider"] == "deepseek-web")
        assert web["cost_status"] == "calculated" and web["cost_microunits"] == 750

    asyncio.run(run())


def test_language_keeps_cost_audit_with_amount_caps_disabled(world, monkeypatch):
    from test_language import service, submitted
    language = service(world, monkeypatch, cost_limits_enabled=False, task_cost_microunits=0, daily_cost_microunits=0)

    async def run():
        await language.submit(world.owner, submitted(world))
        assert await language.process_one()
        call = (await rows(world))[0]
        assert call["cost_status"] == "calculated" and call["cost_microunits"] > 0
        assert call["reserved_microunits"] == language.config.model_reservation(input_tokens=16384, output_tokens=512)

    asyncio.run(run())
