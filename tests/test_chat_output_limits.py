"""DSH-sized output ceilings, reserved costs and non-executable truncation."""

import asyncio
import json

import httpx2
import pytest
from pydantic import SecretStr, ValidationError

from nexus.contracts.basic import FilePreviewArguments
from nexus.core.models.chat import DeepSeekChatProvider, build_request
from nexus.core.models.config import ModelConfig
from nexus.core.models.deepseek import ProviderFailure
from nexus.core.security.types import AccessDenied
from test_chat import Stream, app_for, conversation, rows, send, sse


def test_dsh_ceiling_is_clamped_by_each_reservation_and_integer_cost():
    config = ModelConfig(enabled=True, chat_enabled=True)
    assert config.chat_max_output_tokens == 256_000
    assert config.chat_output_limit() == 33_474
    messages = [{"role": "user", "content": "Write a page"}]
    for reservation in (50_000, 30_000, 10_000):
        limit = build_request(config, messages, output_budget_microunits=reservation)["max_tokens"]
        assert (32768*config.input_price + limit*config.output_price + 999999)//1_000_000 <= reservation
        assert (32768*config.input_price + (limit+1)*config.output_price + 999999)//1_000_000 > reservation
    assert build_request(config, messages, compact=True)["max_tokens"] == 1024
    assert build_request(ModelConfig(chat_max_output_tokens=2048), messages)["max_tokens"] == 2048
    with pytest.raises(ProviderFailure, match="MODEL_BUDGET_INVALID"):
        build_request(config, messages, output_budget_microunits=9830)
    with pytest.raises(ValidationError):
        ModelConfig(enabled=True, chat_enabled=True, input_price=2_000_000)


def test_complete_long_file_call_is_accepted_above_old_token_limit():
    arguments = {"root_id": "workspace", "path": "school.html", "content": "<!doctype html>" + "x"*10_000}
    call = {"index": 0, "id": "long-write", "type": "function", "function": {
        "name": "write_file", "arguments": json.dumps(arguments)}}
    chunks = [sse(delta={"tool_calls": [call]}),
              sse(finish="tool_calls", usage={"prompt_tokens": 4371, "completion_tokens": 8000}), b"data: [DONE]\n\n"]
    captured = []

    async def handle(request):
        captured.append(json.loads(request.content))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream(chunks))

    async def run():
        provider = DeepSeekChatProvider(SecretStr("synthetic-no-network"), ModelConfig(), transport=httpx2.MockTransport(handle))

        async def receive(part):
            pass

        reply = await provider.invoke([{"role": "user", "content": "Write a page"}], receive, allow_tools=True,
            work_catalog=[{"name": "write_file", "description": "Write", "parameters": FilePreviewArguments.model_json_schema()}])
        assert reply.complete and reply.output_tokens == 8000
        assert reply.proposal["arguments"] == arguments
        assert len(reply.work_calls) == 1

    asyncio.run(run())
    assert captured[0]["max_tokens"] == 33_474


def test_zero_budget_rejects_submission_without_provider_or_ledger(world, monkeypatch):
    chat = app_for(world, monkeypatch, task_cost_microunits=0).state.chat

    async def run():
        conv = await conversation(chat, world)
        with pytest.raises(AccessDenied) as caught:
            await send(chat, world, conv["conversation_id"])
        assert caught.value.code == "BUDGET_EXCEEDED"
        assert chat.provider.requests == [] and not await rows(world)

    asyncio.run(run())


@pytest.mark.parametrize("arguments", ['{"root_id":"workspace","path":"school.html","content":"unfinished', '{}'])
def test_length_stop_preserves_usage_but_never_dispatches_batch(world, monkeypatch, arguments):
    app = app_for(world, monkeypatch, chat_max_output_tokens=2048)
    chat = app.state.chat
    chunks = [sse("Preparing page"), sse(delta={"tool_calls": [
        {"index": 0, "id": "valid-first", "type": "function", "function": {"name": "get_system_info", "arguments": "{}"}},
        {"index": 1, "id": "partial-write", "type": "function", "function": {"name": "write_file", "arguments": arguments}},
    ]}), sse(finish="length", usage={"prompt_tokens": 4371, "completion_tokens": 2048}), b"data: [DONE]\n\n"]

    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            response = chunks
        else:
            assert "tools" not in requests[-1]
            response = [sse("输出达到长度上限，这批工具没有执行，页面尚未保存。"),
                        sse(finish="stop", usage={"prompt_tokens": 200, "completion_tokens": 30}), b"data: [DONE]\n\n"]
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream(response))

    async def run():
        from test_chat_work import setup
        _, _, turn = await setup(app, world, [])
        # setup installs a fake provider; use the actual parser for this exchange.
        chat.provider = DeepSeekChatProvider(SecretStr("synthetic-no-network"), chat.config, transport=httpx2.MockTransport(handle))
        assert await chat.process_one()
        done = await chat.get(world.owner, turn["turn_id"])
        assert done["status"] == "incomplete" and done["error_code"] == "MODEL_OUTPUT_LIMIT"
        assert done["response_text"] == "Preparing page"
        assert done["work_steps"] == [] and not await rows(world, "tasks")
        calls = await rows(world)
        assert len(calls) == 1 and calls[0]["cost_status"] == "calculated"
        assert calls[0]["output_tokens"] == 2048
        exchange = (await chat.journal.detail(world.owner, turn["turn_id"]))["exchanges"][0]
        response = json.loads(exchange["response_json"])
        assert response["finish_reason"] == "length" and response["proposal"] is None
        assert response["native_tool_call"] is None and response["work_calls"] == []
        assert await chat.process_one()  # one explanation, never continuation of the truncated tool batch
        explained = await chat.get(world.owner, turn["turn_id"])
        assert explained["error_code"] == "MODEL_OUTPUT_LIMIT"
        assert "没有执行" in explained["presentation"]["final_text"]
        assert not explained["work_steps"] and not await rows(world, "tasks")
        assert not await chat.process_one()
        assert len(await rows(world)) == len(requests) == 2

    asyncio.run(run())


def test_usage_above_reserved_output_limit_is_rejected_even_below_config_ceiling():
    config = ModelConfig()
    reservation = 30_000
    reported = config.chat_output_limit(reservation) + 1
    chunks = [sse("Answer"), sse(finish="stop", usage={"prompt_tokens": 100, "completion_tokens": reported}), b"data: [DONE]\n\n"]

    async def handle(request):
        assert json.loads(request.content)["max_tokens"] == reported-1
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream(chunks))

    async def run():
        provider = DeepSeekChatProvider(SecretStr("synthetic-no-network"), config, transport=httpx2.MockTransport(handle))

        async def receive(part):
            pass

        with pytest.raises(ProviderFailure, match="MODEL_USAGE_INVALID"):
            await provider.invoke([{"role": "user", "content": "Hello"}], receive, output_budget_microunits=reservation)

    asyncio.run(run())


def test_large_text_stream_survives_storage_and_journal(world, monkeypatch):
    # Exceeds both the old 32 KiB text cap and the old 4096-event cap.
    content = "0123456789"*4250
    provider_body = [sse(content[i:i+10]) for i in range(0, len(content), 10)]
    provider_body.extend([sse(finish="stop", usage={"prompt_tokens": 100, "completion_tokens": 12_000}), b"data: [DONE]\n\n"])

    async def handle(request):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream(provider_body))

    chat = app_for(world, monkeypatch).state.chat
    chat.provider = DeepSeekChatProvider(SecretStr("synthetic-no-network"), chat.config, transport=httpx2.MockTransport(handle))

    async def run():
        conv = await conversation(chat, world)
        turn, _ = await send(chat, world, conv["conversation_id"])
        await chat.process_one()
        done = await chat.get(world.owner, turn["turn_id"])
        assert done["status"] == "completed" and done["response_text"] == content
        exchange = (await chat.journal.detail(world.owner, turn["turn_id"]))["exchanges"][0]
        assert json.loads(exchange["response_json"])["content"] == content
        assert json.loads(exchange["request_body"])["max_tokens"] == chat.config.chat_output_limit()

    asyncio.run(run())


def test_work_continuation_uses_reduced_budget_in_provider_and_saved_body(world, monkeypatch):
    from test_chat_work import setup
    chat_app = app_for(world, monkeypatch)
    chat = chat_app.state.chat

    async def run():
        _, _, turn = await setup(chat_app, world, [{"intent": "get_node_status", "arguments": {}}])
        await chat.process_one()
        await chat.process_one()
        calls = await rows(world)
        assert len(calls) == 2
        assert calls[1]["reserved_microunits"] == 50_000 - calls[0]["cost_microunits"]
        exchanges = (await chat.journal.detail(world.owner, turn["turn_id"]))["exchanges"]
        caps = [json.loads(exchange["request_body"])["max_tokens"] for exchange in exchanges]
        assert caps[1] < caps[0]
        assert caps[1] == chat.config.chat_output_limit(calls[1]["reserved_microunits"])
        assert chat.provider.calls[1][1]["output_budget_microunits"] == calls[1]["reserved_microunits"]

    asyncio.run(run())
