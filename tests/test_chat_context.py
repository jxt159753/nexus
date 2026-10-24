"""Context admission and projections preserve execution evidence and native pairs."""

import asyncio
import copy
import json
from uuid import UUID, uuid4

import pytest

from nexus.contracts.extensions import canonical
from nexus.core.cognitive.chat_context import fit_work_context, project_result, request_size
from nexus.core.cognitive.work_catalog import checked, compact_schema, functions
from nexus.core.models.chat import request_body
from nexus.core.models.config import ModelConfig
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one
from test_chat import app_for as chat_app, conversation, rows, send
from test_chat_skills import app_for, prepare
from test_chat_work import basic, file_task, setup


def native(name, arguments, identifier="call-1"):
    return {"id":identifier,"type":"function","function":{"name":name,"arguments":canonical(arguments)}}


def test_size_is_exact_serialized_provider_body_including_schemas_and_title():
    config = ModelConfig()
    messages = [{"role":"user","content":"你好\n\"🙂"*80}]
    catalog = [{"name":"read_file","description":"读取","parameters":{"type":"object","properties":{}}}]
    options = {"allow_tools":True,"work_catalog":catalog,"generate_title":True,"work_limit":2}
    body = request_body(config,messages,**options)
    assert request_size(config,messages,**options) == len(body)
    value = json.loads(body)
    assert "Remaining native actions in this request: 2" in value["messages"][0]["content"]
    assert value["tools"][0]["function"]["name"] == "read_file"
    assert "Use web_search" not in value["messages"][0]["content"]
    assert "Use inspect_capability" not in value["messages"][0]["content"]


def test_history_trimming_keeps_current_memories_and_all_native_pairs():
    config = ModelConfig()
    call = native("get_node_status",{})
    current = [{"role":"user","content":"本轮选定的记忆"}, {"role":"user","content":"当前请求"},
               {"role":"assistant","content":None,"tool_calls":[call]},
               {"role":"tool","tool_call_id":call["id"],"content":canonical({"status":"succeeded","data":{"text":"原始结果"*2000}})}]
    messages = [{"role":"user","content":"旧问题"},{"role":"assistant","content":"旧答复"*5000},*current]
    before = copy.deepcopy(messages)
    projected = fit_work_context(config,messages,[])
    assert projected == current and messages == before
    assert request_size(config,projected,allow_tools=True,work_catalog=[]) <= 32768


def test_configured_capacity_can_remove_exact_old_summary_before_whole_rounds():
    from nexus.core.cognitive.chat_context import history_message
    config=ModelConfig(chat_input_bytes=8192)
    summary=[{"turn_id":str(uuid4()),"user_excerpt":"x"*4000,"assistant_excerpt":"y"*4000}]
    current=[{"role":"user","content":"当前明确选定资料"},{"role":"user","content":"当前任务"}]
    messages=[history_message(summary),{"role":"user","content":"旧问题"},{"role":"assistant","content":"旧答复"*2000},*current]
    result=fit_work_context(config,messages,[],history_summary=summary)
    assert result==current
    assert messages[0]==history_message(summary)


def test_result_projection_preserves_raw_evidence_status_sources_and_call_ids():
    config = ModelConfig()
    calls = [native("write_file",{"content":"x"*10000},"write"),native("web_fetch",{"url":"https://example.com/"},"web")]
    result = {"status":"succeeded","verification":"provider_response_checked", "data":{"url":"https://example.com/", "content":"首🙂"*4000+"尾🙂", "truncated":False}}
    messages = [{"role":"user","content":"当前输入"*300}, {"role":"assistant","content":None,"tool_calls":calls},
                *[{"role":"tool","tool_call_id":call["id"],"content":canonical(result)} for call in calls]]
    before = copy.deepcopy(messages)
    projected = fit_work_context(config,messages,[])
    assert projected[0:2] == messages[0:2] and messages == before
    assert [message["tool_call_id"] for message in projected[2:]] == ["write","web"]
    shortened = [json.loads(message["content"]) for message in projected[2:]]
    excerpts = [item for item in shortened if "context_projection" in item]
    assert excerpts and all(item["status"] == "succeeded" and item["verification"] == result["verification"] for item in excerpts)
    assert all(item["source_url"] == result["data"]["url"] and "尾🙂" in item["data_excerpt"] for item in excerpts)
    assert request_size(config,projected,allow_tools=True,work_catalog=[]) <= 32768


def test_current_input_and_native_arguments_are_never_truncated_to_force_admission():
    messages = [{"role":"user","content":"x"*33000}]
    with pytest.raises(AccessDenied,match="MODEL_INPUT_LIMIT"):
        fit_work_context(ModelConfig(),messages,[])
    assert len(messages[0]["content"]) == 33000


def test_projection_is_explicit_and_preserves_failed_result_metadata():
    result = {"status":"failed","verification":None,"error_code":"COMMAND_FAILED","data":{"stdout":"x"*18000,"stderr":"failure at end"}}
    projected = project_result(result,1024)
    assert projected["status"] == "failed" and projected["error_code"] == "COMMAND_FAILED"
    assert "failure at end" in projected["data_excerpt"]
    assert "context_projection" not in result and projected["context_projection"]["truncated"]


def test_compact_schemas_keep_fields_named_title_and_all_validation_constraints():
    schema = {"type":"object","title":"Outer","properties":{"title":{"title":"Title","type":"string","minLength":1}},
              "required":["title"],"additionalProperties":False,"$defs":{"nested":{"title":"Nested","type":"integer","minimum":2}}}
    original = copy.deepcopy(schema)
    compact = compact_schema(schema)
    assert compact["properties"]["title"] == {"type":"string","minLength":1}
    assert compact["required"] == ["title"] and compact["additionalProperties"] is False
    assert compact["$defs"]["nested"]["minimum"] == 2 and schema == original


def test_capability_catalog_defers_exact_schema_but_validates_against_full_snapshot():
    identifier = str(uuid4())
    entry = {"name":"run_tool","version_id":identifier,"artifact_digest":"a"*64,"capability_id":"user.test", "description":"测试", "input_schema":{"type":"object","properties":{"count":{"type":"integer"}},"required":["count"],"additionalProperties":False}}
    tools = functions([entry])
    assert [tool["function"]["name"] for tool in tools] == ["inspect_capability","run_tool"]
    assert "input_schema" not in tools[1]["function"]["description"]
    proposal = {"intent":"inspect_capability","arguments":{"version_id":identifier}}
    assert checked([entry],proposal,native("inspect_capability",proposal["arguments"])) == proposal
    proposal["arguments"]["version_id"] = str(uuid4())
    with pytest.raises(AccessDenied):
        checked([entry],proposal,native("inspect_capability",proposal["arguments"]))
    with pytest.raises(ValueError):
        checked([entry],{"intent":"run_tool","arguments":{"version_id":identifier,"input":{}}},native("run_tool",{"version_id":identifier,"input":{}}))


@pytest.mark.parametrize("disable",[False,True])
def test_capability_inspection_is_bounded_authorized_and_does_not_execute(world,monkeypatch,disable):
    app = app_for(world,monkeypatch)
    async def run():
        version = await prepare(app,world)
        chat = app.state.chat
        _,_,turn = await setup(app,world,[{"intent":"inspect_capability","arguments":{"version_id":version["version_id"]}}])
        await chat.process_one()
        if disable:
            await app.state.extensions.disable(world.owner,UUID(version["version_id"]))
        await chat.process_one()
        done = await chat.get(world.owner,turn["turn_id"])
        assert not await rows(world,"tasks") and not await rows(world,"skill_runs")
        if disable:
            assert done["status"] == "stopped" and done["work_steps"][0]["result"] is None
        else:
            step = done["work_steps"][0]
            assert step["result"]["data"]["input_schema"] and step["result"]["verification"] == "approved_version_snapshot"
            assert done["model_call_count"] == 2 and step["status"] == "succeeded"
            assert (await chat.history(world.owner,turn["conversation_id"]))["work_grant"]["used_operations"] == 1
    asyncio.run(run())


def test_oversized_local_request_is_rejected_before_reserving_or_calling_model(world,monkeypatch):
    chat = chat_app(world,monkeypatch).state.chat
    async def run():
        conv = await conversation(chat,world)
        turn,_ = await send(chat,world,conv["conversation_id"])
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE chat_turns SET context_json=? WHERE turn_id=?",(canonical([{"role":"user","content":"x"*33000}]),turn["turn_id"]))
        await chat.process_one()
        done = await chat.get(world.owner,turn["turn_id"])
        assert done["status"] == "failed" and done["error_code"] == "MODEL_INPUT_LIMIT"
        assert not await rows(world) and chat.provider.requests == []
    asyncio.run(run())


def test_full_tool_evidence_survives_when_current_request_cannot_fit(world,monkeypatch):
    app = app_for(world,monkeypatch)
    async def run():
        chat = app.state.chat
        _,_,turn = await setup(app,world,[{"intent":"get_node_status","arguments":{}}])
        await chat.process_one()
        async with world.db.transaction() as conn:
            row = await one(conn,"SELECT work_messages_json FROM chat_turns WHERE turn_id=?",(turn["turn_id"],))
            messages = json.loads(row[0]); messages[0]["content"] = "x"*33000
            await conn.execute("UPDATE chat_turns SET work_messages_json=? WHERE turn_id=?",(canonical(messages),turn["turn_id"]))
        await chat.process_one()
        done = await chat.get(world.owner,turn["turn_id"])
        assert done["error_code"] == "MODEL_INPUT_LIMIT"
        assert done["work_steps"][0]["status"] == "succeeded"
        assert done["work_steps"][0]["result"]["data"]["online"] is False
    asyncio.run(run())


def test_four_actual_file_results_are_retained_while_model_context_is_compacted(world,monkeypatch,tmp_path):
    app = app_for(world,monkeypatch)
    root = tmp_path / "workspace"
    root.mkdir()
    content = "HEAD\n" + "x"*8100 + "\nTAIL"
    (root / "large.txt").write_text(content,encoding="utf-8",newline="\n")
    async def run():
        await basic(app,world)
        chat = app.state.chat
        _,_,turn = await setup(app,world,[{"intent":"read_file","arguments":{"root_id":"workspace","path":"large.txt"}}]*4)
        for _ in range(4):
            await chat.process_one()
            await chat.process_one()
            current = await chat.get(world.owner,turn["turn_id"])
            step = current["work_steps"][-1]
            await file_task(app,world,step["task_id"],root)
        await chat.process_one()
        done = await chat.get(world.owner,turn["turn_id"])
        assert done["status"] == "completed" and done["model_call_count"] == 5
        assert done["context_truncated"] and len(done["work_steps"]) == 4
        assert all(step["status"] == "succeeded" and step["result"]["data"]["text"] == content for step in done["work_steps"])
        results = [json.loads(message["content"]) for message in done["work_messages"] if message["role"] == "tool"]
        assert any(result.get("context_projection",{}).get("truncated") for result in results)
        assert len({message["tool_call_id"] for message in done["work_messages"] if message["role"] == "tool"}) == 4
        assert (root / "large.txt").read_text(encoding="utf-8") == content
    asyncio.run(run())
