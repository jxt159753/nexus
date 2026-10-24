"""Invalid native arguments are visible, never executed and bounded by five exchanges."""

import asyncio
import json

import httpx2
import pytest
from pydantic import SecretStr

from nexus.contracts.basic import FilePreviewArguments
from nexus.contracts.work import SkillCreateArguments, WORK_PROPOSAL
from nexus.core.models.tool_validation import argument_error
from nexus.core.models.chat import DeepSeekChatProvider
from nexus.core.models.config import ModelConfig
from test_chat import Stream, app_for, rows, sse
from test_chat_work import basic, file_task
from test_workspace_work import send_workspace


def test_native_skill_error_keeps_nested_path_and_static_repair_hint():
    from pydantic import ValidationError
    from nexus.plugins.catalog import RELEASES
    arguments={'reason':'create reusable check','skill':{'kind':'skill',
        'manifest':{'kind':'skill','capability_id':'user.mcp_health','description':'fixed MCP health check',
            'input_schema':{'type':'object','properties':{},'additionalProperties':False},
            'output_schema':{'type':'object','properties':{},'additionalProperties':True},
            'verification_profile':'skill_native_steps_v1'},'markdown':'check',
        'steps':[{'id':'check','tool':'plugin.call','plugin_id':'builtin.mcp',
            'plugin_tool':'mcp_call_tool','plugin_digest':RELEASES['builtin.mcp'].digest(),'arguments':{}}], 'tests':[]}}
    with pytest.raises(ValidationError) as failure:
        WORK_PROPOSAL.validate_json(json.dumps({'intent':'skill_create','arguments':arguments}))
    error=argument_error(0,'device1__skill_create',arguments,SkillCreateArguments.model_json_schema(),failure.value)
    assert len(error['issues'])==1
    assert error['issues'][0]['field'].endswith('skill.steps.0')
    assert 'tool_version: 1' in error['issues'][0]['hint']
    assert 'extra_forbidden' not in json.dumps(error)


def native(name, arguments, *, index=0, identifier="write-1"):
    return {"index":index,"id":identifier,"type":"function","function":{"name":name,"arguments":arguments}}


def response(calls=None, text=None):
    chunks = [sse(text)] if text else []
    if calls is not None:
        # Real providers split arguments over many events, including large files.
        for call in calls:
            arguments=call["function"]["arguments"]
            chunks.append(sse(delta={"tool_calls":[{**call,"function":{**call["function"],"arguments":""}}]}))
            chunks.extend(sse(delta={"tool_calls":[{"index":call["index"],"function":{"arguments":arguments[i:i+500]}}]}) for i in range(0,len(arguments),500))
    chunks += [sse(finish="tool_calls" if calls is not None else "stop",usage={"prompt_tokens":100,"completion_tokens":4223}),b"data: [DONE]\n\n"]
    return httpx2.Response(200,headers={"content-type":"text/event-stream"},stream=Stream(chunks))


@pytest.mark.parametrize("arguments", [
    json.dumps({"root_id":"workspace","path":"school.html","content":"中"*4001},ensure_ascii=False),
    json.dumps({"root_id":"workspace","path":"school.html","content":"x"*12001}),
    '{"root_id":"workspace","path":"school.html","content":',
])
def test_arguments_rejected_with_safe_details_and_no_proposal(arguments):
    async def handle(request):
        return response([native("write_file",arguments)])

    async def run():
        provider=DeepSeekChatProvider(SecretStr("synthetic-no-network"),ModelConfig(),transport=httpx2.MockTransport(handle))
        async def receive(text): pass
        reply=await provider.invoke([{"role":"user","content":"写网页"}],receive,allow_tools=True,
            work_catalog=[{"name":"write_file","description":"Write","parameters":FilePreviewArguments.model_json_schema()}])
        assert reply.complete and reply.proposal is None and not reply.work_calls
        assert len(reply.rejected_calls)==1 and reply.output_tokens==4223
        error=reply.validation_errors[0]
        assert error["error_code"] == ("FILE_CONTENT_TOO_LARGE" if arguments.endswith("}") else "INVALID_TOOL_ARGUMENTS")
        assert "school.html" not in json.dumps(error)
        if arguments.endswith("}"):
            assert error["content_utf8_bytes"] == (12003 if "中" in arguments else 12001)
            assert error["max_content_utf8_bytes"]==12000
    asyncio.run(run())


def test_rejected_whole_batch_then_corrected_write_uses_preview_and_readback(world,monkeypatch,tmp_path):
    app=app_for(world,monkeypatch)
    folder=tmp_path/"workspace";folder.mkdir()
    captured=[]
    bad=json.dumps({"root_id":"workspace","path":"school.html","content":"中"*4001},ensure_ascii=False)
    good="<!doctype html><title>泰安一中</title><h1>泰安一中</h1>"

    async def handle(request):
        body=json.loads(request.content);captured.append(body)
        if len(captured)==1:
            return response([native("get_system_info","{}",identifier="read-1"),native("write_file",bad,index=1)])
        if len(captured)==2:
            feedback=[json.loads(m["content"]) for m in body["messages"][-2:]]
            assert all(item["execution_outcome"]=="not_started" and item["batch_executed"] is False for item in feedback)
            assert feedback[1]["errors"][0]["content_utf8_bytes"]==12003
            return response([native("write_file",json.dumps({"root_id":"workspace","path":"school.html","content":good}),identifier="write-2")])
        return response(text="页面已写入，并经文件工具读回校验。")

    async def run():
        await basic(app,world)
        chat=app.state.chat
        turn=await send_workspace(app,world,[])
        chat.provider=DeepSeekChatProvider(SecretStr("synthetic-no-network"),chat.config,transport=httpx2.MockTransport(handle))
        assert await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        assert current["status"]=="queued" and current["work_steps"]==[]
        assert not await rows(world,"tasks") and not list(folder.iterdir())
        assert await chat.process_one(); await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        step=current["work_steps"][0]
        await file_task(app,world,step["preview_task_id"],folder)
        await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        assert current["work_status"]!="permission"
        await file_task(app,world,current["work_steps"][0]["task_id"],folder)
        await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="completed" and len(done["work_steps"])==1
        assert (folder/"school.html").read_text(encoding="utf-8")==good
        log=(await chat.journal.detail(world.owner,turn["turn_id"]))["exchanges"]
        assert [r["outcome"] for r in log]==["failed","completed","completed"]
        assert json.loads(log[0]["response_json"])["validation_errors"][0]["content_utf8_bytes"]==12003
        assert all(r["cost_status"]=="calculated" for r in await rows(world))
        assert len(captured)==3 and len(await rows(world,"tasks"))==2
    asyncio.run(run())


def test_repeated_argument_failures_keep_original_five_exchange_limit(world,monkeypatch):
    app=app_for(world,monkeypatch);captured=[]
    async def handle(request):
        body=json.loads(request.content);captured.append(body)
        if "tools" not in body:
            return response(text="文件参数始终未通过校验，未创建文件。")
        return response([native("write_file",'{"root_id":"workspace"}',identifier=f"bad-{len(captured)}")])
    async def run():
        await basic(app,world)
        chat=app.state.chat;turn=await send_workspace(app,world,[])
        chat.provider=DeepSeekChatProvider(SecretStr("synthetic-no-network"),chat.config,transport=httpx2.MockTransport(handle))
        for _ in range(5): assert await chat.process_one()
        assert not await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="completed" and done["work_steps"]==[]
        assert len(captured)==5 and len(await rows(world))==5 and not await rows(world,"tasks")
    asyncio.run(run())


def test_unknown_tool_is_fatal_and_failure_diagnostic_is_saved(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def handle(request):
        return response([native("not-a-supplied-tool",'{"password":"DO-NOT-LOG"}')])
    async def run():
        await basic(app,world)
        chat=app.state.chat;turn=await send_workspace(app,world,[])
        chat.provider=DeepSeekChatProvider(SecretStr("synthetic-no-network"),chat.config,transport=httpx2.MockTransport(handle))
        assert await chat.process_one()
        assert await chat.process_one()  # One explanation-only call; the provider is still malformed.
        assert not await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="failed" and done["error_code"]=="MODEL_RESPONSE_INVALID"
        exchange=(await chat.journal.detail(world.owner,turn["turn_id"]))["exchanges"][0]
        detail=json.loads(exchange["response_json"])
        assert detail["diagnostic"]["stage"]=="tool_envelope"
        assert "DO-NOT-LOG" not in exchange["response_json"]
        assert done["presentation"]["phase"]=="failed" and not await rows(world,"tasks")
    asyncio.run(run())


@pytest.mark.parametrize("control",["cancel","revoke","restart","daily_limit"])
def test_rejected_arguments_do_not_bypass_stop_recovery_or_admission(world,monkeypatch,control):
    app=app_for(world,monkeypatch,**({"daily_calls":1} if control=="daily_limit" else {}))
    requests=[]
    async def handle(request):
        requests.append(request)
        return response([native("write_file",'{"root_id":"workspace"}')])
    async def run():
        await basic(app,world)
        chat=app.state.chat;turn=await send_workspace(app,world,[])
        chat.provider=DeepSeekChatProvider(SecretStr("synthetic-no-network"),chat.config,transport=httpx2.MockTransport(handle))
        await chat.process_one()
        if control=="cancel": await chat.cancel(world.owner,turn["turn_id"])
        if control=="revoke": await chat.work.revoke(world.owner,turn["conversation_id"])
        if control=="restart": await chat.recover()
        assert not await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert len(requests)==1 and len(await rows(world))==1 and not await rows(world,"tasks")
        if control=="restart": assert done["recovery_required"]
        if control=="daily_limit": assert done["error_code"]=="BUDGET_EXCEEDED"
    asyncio.run(run())
