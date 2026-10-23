"""Known operational failures return native tool evidence without weakening stops."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest

from nexus.contracts.errors import ErrorCode, create_error
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import utc_now
from nexus.core.storage.control import expire_task
from nexus.core.storage.execution import ExecutionRepository, RESULT
from nexus.core.storage.tasks import load_task
from test_chat import rows
from test_chat_skills import app_for
from test_chat_work import WorkProvider, basic, setup
from test_extensions_m4 import FakeSandbox, generate, run_approved


@pytest.mark.parametrize("preview", [False, True])
def test_known_task_and_preview_failures_reach_model_with_original_error(world, monkeypatch, preview):
    app = app_for(world, monkeypatch)
    chat = app.state.chat

    async def run():
        await basic(app, world)
        proposal = {"intent": "write_file" if preview else "read_file", "arguments": {
            "root_id": "workspace", "path": "missing/note.txt", **({"content": "new"} if preview else {})}}
        _, _, turn = await setup(app, world, [proposal])
        await chat.process_one()
        await chat.process_one()
        step = (await chat.get(world.owner, turn["turn_id"]))["work_steps"][0]
        task_id = step["preview_task_id"] if preview else step["task_id"]
        repo = ExecutionRepository(world.db, world.security, 32768)
        command = await repo.dispatch(UUID(task_id), world.node, app.state.harness.scope(world.node_id), uuid4(), uuid4(), 10)
        error = create_error(ErrorCode.RESULT_INVALID, "Target parent is unavailable")
        result = RESULT.validate_json(json.dumps({
            **command.model_dump(mode="json", include={"session_id", "task_id", "attempt_id", "tool", "tool_version", "command_digest"}),
            "observed_at": utc_now().isoformat(), "outcome": "failed", "error": error.model_dump(mode="json")}))
        await repo.store_result(world.node, result)
        await repo.verify(command.attempt_id)
        await chat.process_one()
        done = await chat.get(world.owner, turn["turn_id"])
        feedback = json.loads(chat.provider.calls[-1][0][-1]["content"])
        assert feedback["status"] == "failed"
        assert feedback["error_code"] == "RESULT_INVALID"
        assert feedback["error"]["message"] == "Target parent is unavailable"
        assert done["status"] == "completed" and done["work_steps"][0]["status"] == "failed"
        assert done["presentation"]["final_text"] == done["response_text"]
        assert not done["presentation"]["collapse_process"]
        if preview:
            assert feedback["phase"] == "preview" and feedback["write_executed"] is False
            assert done["work_steps"][0]["task_id"] is None
        assert len(await rows(world, "tasks")) == 1
        assert done["model_call_count"] == 2
    asyncio.run(run())


@pytest.mark.parametrize("error", [ValueError("bounded rejection"), AccessDenied("NODE_OFFLINE"), AccessDenied("POLICY_DENIED")])
def test_pre_execution_errors_are_reported_and_policy_denials_get_tool_free_explanation(world, monkeypatch, error):
    app = app_for(world, monkeypatch)
    chat = app.state.chat

    async def unavailable(node_id):
        raise error

    async def run():
        _, _, turn = await setup(app, world, [{"intent": "get_node_status", "arguments": {}}])
        monkeypatch.setattr(chat.harness.link, "node_status", unavailable)
        await chat.process_one()
        await chat.process_one()
        done = await chat.get(world.owner, turn["turn_id"])
        if isinstance(error, AccessDenied) and error.code == "POLICY_DENIED":
            assert done["status"] == "completed" and done["model_call_count"] == 2
            assert done["error_code"] == "POLICY_DENIED"
            assert done["work_steps"][0]["status"] == "stopped"
            assert chat.provider.calls[-1][1]["allow_tools"] is False
        else:
            feedback = json.loads(chat.provider.calls[-1][0][-1]["content"])
            assert feedback["status"] == "failed" and feedback["execution_outcome"] == "not_started"
            assert feedback["error_code"] == (error.code if isinstance(error, AccessDenied) else "WORK_ACTION_UNAVAILABLE")
            assert done["status"] == "completed" and done["model_call_count"] == 2
        assert not await rows(world, "tasks")
    asyncio.run(run())


def test_failed_generated_tool_includes_controller_error(world, monkeypatch):
    app = app_for(world, monkeypatch, sandbox=True)
    fake = FakeSandbox()
    app.state.extensions.sandbox.transport = fake
    chat = app.state.chat

    async def run():
        version = await run_approved(app, world, await generate(app, world, "tool", "统计目录", node_id=world.node_id))
        await app.state.extensions.enable(world.owner, UUID(version["version_id"]))
        fake.forged = True
        _, _, turn = await setup(app, world, [{"intent": "run_tool", "arguments": {
            "version_id": version["version_id"], "input": {"entries": []}}}])
        await chat.process_one()
        await chat.process_one()
        feedback = json.loads(chat.provider.calls[-1][0][-1]["content"])
        assert feedback["status"] == "failed" and feedback["error_code"] == "RESULT_INVALID"
        assert feedback["error"] == {"code": "RESULT_INVALID"}
        assert (await chat.get(world.owner, turn["turn_id"]))["work_steps"][0]["status"] == "failed"
    asyncio.run(run())


def test_unknown_preview_stops_without_model_retry(world, monkeypatch):
    app = app_for(world, monkeypatch)
    chat = app.state.chat

    async def run():
        await basic(app, world)
        _, _, turn = await setup(app, world, [{"intent": "write_file", "arguments": {
            "root_id": "workspace", "path": "note.txt", "content": "new"}}])
        await chat.process_one()
        await chat.process_one()
        step = (await chat.get(world.owner, turn["turn_id"]))["work_steps"][0]
        repo = ExecutionRepository(world.db, world.security, 32768)
        await repo.dispatch(UUID(step["preview_task_id"]), world.node, app.state.harness.scope(world.node_id), uuid4(), uuid4(), 10)
        async with world.db.transaction() as conn:
            task = await load_task(conn, UUID(step["preview_task_id"]))
            await expire_task(conn, task, task.expires_at)
        await chat.process_one()
        done = await chat.get(world.owner, turn["turn_id"])
        assert done["status"] == "stopped" and done["error_code"] == "EXECUTION_UNKNOWN"
        assert done["work_steps"][0]["result"]["execution_outcome"] == "unknown"
        assert done["work_steps"][0]["task_id"] is None
        assert done["model_call_count"] == 1 and len(await rows(world, "tasks")) == 1
    asyncio.run(run())
