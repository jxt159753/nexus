"""DSH tool adaptations retain native pairing, grants and source visibility."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest

from nexus.core.cognitive import work_catalog
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, utc_now
from test_chat_skills import app_for, prepare
from test_chat_work import setup, basic, file_task


def test_current_time_is_a_native_audited_query(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        _, _, turn = await setup(app, world, [{"intent":"get_current_time", "arguments":{"utc_offset_minutes":480}}])
        await chat.process_one(); await chat.process_one()
        result = (await chat.get(world.owner, turn["turn_id"]))["work_steps"][0]["result"]
        assert result["verification"] == "core_record_snapshot" and result["data"]["local"].endswith("+08:00")
        assert result["data"]["utc"].endswith("+00:00") and result["data"]["elapsed_since_turn_seconds"] >= 0
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT count(*) FROM chat_work_operations"))[0] == 1
            assert (await one(conn, "SELECT count(*) FROM tasks"))[0] == 0
    asyncio.run(run())


def test_jobs_query_uses_current_tasks_and_cannot_read_another_turn(world, monkeypatch, tmp_path):
    from nexus.core.cognitive.work_queries import query
    app = app_for(world, monkeypatch)
    (tmp_path/"file.txt").write_text("actual output", encoding="utf-8")
    async def run():
        await basic(app, world)
        chat = app.state.chat
        _, _, turn = await setup(app, world, [{"intent":"read_file", "arguments":{"root_id":"workspace", "path":"file.txt"}},
                                             {"intent":"job_list", "arguments":{"limit":1}}])
        await chat.process_one(); await chat.process_one()
        current = await chat.get(world.owner, turn["turn_id"])
        task_id = current["work_steps"][0]["task_id"]
        await file_task(app, world, task_id, tmp_path)
        chat.provider.actions.append({"intent":"job_output", "arguments":{"task_id":task_id,"max_bytes":4096}})
        for _ in range(5):
            await chat.process_one()
        done = await chat.get(world.owner, turn["turn_id"])
        jobs = done["work_steps"][1]["result"]["data"]
        assert jobs["jobs"][0]["task_id"] == task_id and jobs["jobs"][0]["status"] == "succeeded"
        assert "actual output" not in json.dumps(jobs)
        assert done["work_steps"][2]["result"]["data"]["output"]["data"]["text"] == "actual output"
        _, _, other_turn = await setup(app, world, [])
        async with world.db.transaction() as conn:
            step = await one(conn,"SELECT * FROM chat_work_steps WHERE step_id=?",(done["work_steps"][1]["step_id"],))
            grant = await one(conn,"SELECT * FROM chat_work_grants WHERE grant_id=(SELECT work_grant_id FROM chat_turns WHERE turn_id=?)",(turn["turn_id"],))
            result = await query(conn, step, grant, "job_output", {"task_id":task_id, "max_bytes":4096}, utc_now())
            assert result["output"]["data"]["text"] == "actual output"
            with pytest.raises(AccessDenied):
                await query(conn, step, grant, "job_output", {"task_id":str(uuid4()), "max_bytes":4096}, utc_now())
            other_grant = await one(conn,"SELECT * FROM chat_work_grants WHERE grant_id=(SELECT work_grant_id FROM chat_turns WHERE turn_id=?)",(other_turn["turn_id"],))
            with pytest.raises(AccessDenied):
                await query(conn, {"turn_id":other_turn["turn_id"]}, other_grant, "job_output", {"task_id":task_id,"max_bytes":4096}, utc_now())
            with pytest.raises(AccessDenied):
                await query(conn, step, {**dict(grant), "node_id":str(uuid4())}, "job_output", {"task_id":task_id,"max_bytes":4096}, utc_now())
    asyncio.run(run())


def test_capability_inspection_loads_approved_skill_instructions(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        version = await prepare(app, world)
        chat = app.state.chat
        _, _, turn = await setup(app, world, [{"intent":"inspect_capability", "arguments":{"version_id":version["version_id"]}}])
        await chat.process_one(); await chat.process_one()
        result = (await chat.get(world.owner, turn["turn_id"]))["work_steps"][0]["result"]
        assert result["data"]["markdown"] and len(result["data"]["steps"]) == 2
        assert result["verification"] == "approved_version_snapshot"
        async with world.db.transaction(write=False) as conn:
            assert not await one(conn,"SELECT 1 FROM tasks")
    asyncio.run(run())


def test_images_require_configuration_and_both_identity_grants(world, monkeypatch):
    from nexus.core.config import TrustedNode
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        app.state.harness.link.config.nodes[world.node_id] = TrustedNode(tools=frozenset({"filesystem.read_image@1"}),roots=frozenset({"workspace"}))
        await world.security.set_grant(world.owner_id,"filesystem.read_image",root_id="workspace")
        async def visible():
            async with world.db.transaction(write=False) as conn:
                return {entry["name"] for entry in (await work_catalog.catalog(chat,conn,world.owner,world.node_id))["catalog"]}
        assert "read_image" not in await visible()
        chat.config.chat_images_enabled = True
        assert "read_image" not in await visible()
        await world.security.set_grant(world.node_id,"filesystem.read_image",root_id="workspace")
        assert "read_image" in await visible()
    asyncio.run(run())


def test_image_batch_reaches_model_after_all_native_results_and_retains_journal(world, monkeypatch, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from nexus.core.config import TrustedNode
    from nexus.core.models.chat import request_body
    from nexus.core.models.providers import ModelReply
    from nexus.core.storage.execution import ExecutionRepository, RESULT
    from nexus.node.tools.images import read_image
    from nexus.node.tools.files import read_file
    Image.new("RGB", (64, 32), "red").save(tmp_path/"test.png")
    (tmp_path/"note.txt").write_text("a verified note", encoding="utf-8")
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        chat.config.chat_images_enabled = True
        tools = frozenset({"filesystem.read_image@1", "filesystem.read@1"})
        app.state.harness.link.config.nodes[world.node_id] = TrustedNode(tools=tools, roots=frozenset({"workspace"}))
        for key in tools:
            for identity in (world.owner_id,world.node_id):
                await world.security.set_grant(identity,key[:-2],root_id="workspace")
        _, _, turn = await setup(app, world, [])
        calls = []
        for index, proposal in enumerate([
            {"intent":"read_image", "arguments":{"root_id":"workspace","path":"test.png"}},
            {"intent":"read_file", "arguments":{"root_id":"workspace","path":"note.txt"}},
        ]):
            calls.append((proposal,{"id":f"batch-{index}","type":"function","function":{"name":proposal["intent"],"arguments":json.dumps(proposal["arguments"])}}))
        class Batch:
            def __init__(self): self.calls=[]
            async def invoke(self,messages,receive,**options):
                self.calls.append(messages)
                if len(self.calls) == 1:
                    return ModelReply("读取图片与说明。",100,20,True,finish_reason="tool_calls",proposal=calls[0][0],native_tool_call=calls[0][1],work_calls=tuple(calls))
                assert [item["role"] for item in messages[-4:]] == ["assistant","tool","tool","user"]
                assert [item["tool_call_id"] for item in messages[-3:-1]] == ["batch-0","batch-1"]
                assert "data_base64" not in messages[-3]["content"]
                assert messages[-1]["content"][-1]["image_url"]["url"].startswith("data:image/png;base64,")
                body = request_body(chat.config,messages,**options)
                assert len(body) <= 32768
                return ModelReply("收到真实图片和说明。",1200,20,True)
        chat.provider = Batch()
        for _ in range(12):
            await chat.process_one()
            current = await chat.get(world.owner,turn["turn_id"])
            for step in current["work_steps"]:
                if not step["task_id"]:
                    continue
                task = await world.tasks.get(UUID(step["task_id"]),world.owner_id)
                if task.status != "ready":
                    continue
                repo = ExecutionRepository(world.db,world.security,32768)
                command = await repo.dispatch(task.task_id,world.node,app.state.harness.scope(world.node_id),uuid4(),uuid4(),10)
                output = (read_image if command.tool == "filesystem.read_image" else read_file)(command.arguments,tmp_path)
                result = RESULT.validate_json(json.dumps({**command.model_dump(mode="json",include={"session_id","task_id","attempt_id","tool","tool_version","command_digest"}),"observed_at":utc_now().isoformat(),"outcome":"succeeded","result":output.model_dump(mode="json")}))
                await repo.store_result(world.node,result); await repo.verify(command.attempt_id)
            if current["status"] == "completed" and not current["work_status"]:
                break
        done = await chat.get(world.owner,turn["turn_id"])
        assert done["status"] == "completed" and done["model_call_count"] == 2
        assert done["work_steps"][0]["result"]["data"]["image"]["data_base64"]
        async with world.db.transaction(write=False) as conn:
            journal = await conn.execute_fetchall("SELECT request_body FROM chat_exchanges WHERE turn_id=? ORDER BY rowid",(turn["turn_id"],))
            assert len(journal) == 2 and "data:image/png;base64," in journal[-1][0]
    asyncio.run(run())


def test_image_request_gate_token_reservation_and_remote_urls(tmp_path):
    from nexus.contracts.images import ImageReadArguments, image_url
    from nexus.core.cognitive.chat_context import estimated_tokens, pressure
    from nexus.core.models.chat import request_body
    from nexus.core.models.config import ModelConfig
    from nexus.core.models.deepseek import ProviderFailure
    from nexus.node.tools.images import read_image
    Image = pytest.importorskip("PIL.Image")
    Image.new("RGB",(10,10)).save(tmp_path/"test.png")
    image = read_image(ImageReadArguments(root_id="workspace",path="test.png"),tmp_path).image.model_dump(mode="json")
    block = {"type":"image_url","image_url":{"url":image_url(image),"detail":"original"}}
    messages = [{"role":"user","content":[{"type":"text","text":"图片是资料"},block]}]
    config = ModelConfig(chat_images_enabled=True)
    assert estimated_tokens(messages) > 1024 and pressure(config,messages)["fits"]
    assert b"image_url" in request_body(config,messages)
    with pytest.raises(ProviderFailure):
        request_body(ModelConfig(),messages)
    with pytest.raises(ProviderFailure):
        request_body(config,[{**messages[0],"role":"assistant"}])
    block["image_url"]["url"] = "https://example.com/private.png"
    with pytest.raises(ProviderFailure):
        request_body(config,messages)


def test_image_projection_preserves_unrelated_tool_payload():
    from nexus.core.cognitive.work_images import model_result
    result = {"status":"succeeded","data":{"image":{"data_base64":"user data"}}}
    assert model_result(result,is_image=False) == result
    assert "data_base64" not in model_result(result,is_image=True)["data"]["image"]
    assert result["data"]["image"]["data_base64"] == "user data"
