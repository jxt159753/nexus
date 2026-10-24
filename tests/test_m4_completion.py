"""M4 input, idempotency and durable sandbox evidence regressions."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest

from nexus.contracts.extensions import parse_generation
from nexus.contracts.sandbox import SandboxResult, SandboxStatusResult, sandbox_command_digest
from nexus.core.extensions import service as service_module
from nexus.core.extensions.fixtures import fixture_generation
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one
from nexus.core.storage.database import timestamp, utc_now
from nexus.core.storage.tasks import Conflict
from nexus.sandbox.node import Receipts
from test_extensions_m4 import app_for, generate, grants, run_approved, FakeSandbox


def input_skill():
    candidate = json.loads(fixture_generation("skill", "overview", parent_version_id=None, tool_version_id=None))
    candidate["manifest"]["input_schema"] = {
        "type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
        "required": ["limit"], "additionalProperties": False}
    candidate["steps"][1]["arguments"]["limit"] = {"$input": {"field": "limit"}}
    candidate["tests"][0]["input"] = {"limit": 5}
    return candidate


def test_input_reference_is_declared_and_not_an_expression():
    value = input_skill()
    assert parse_generation(json.dumps(value)).steps[1].arguments["limit"] == {"$input": {"field": "limit"}}
    for reference in ({"field": "secret"}, {"field": "limit", "path": "nested"}, {"field": "__import__('os')"}):
        value["steps"][1]["arguments"]["limit"] = {"$input": reference}
        with pytest.raises(ValueError):
            parse_generation(json.dumps(value))


def test_input_validation_precedes_all_tasks_and_resolves_builtin(world, monkeypatch):
    monkeypatch.setattr(service_module, "fixture_generation", lambda *a, **k: json.dumps(input_skill()))
    async def run():
        app = app_for(world)
        await grants(world)
        await grants(world, "filesystem.list")
        version = await run_approved(app, world, await generate(app, world, "skill", "自定义条目数"))
        version_id = UUID(version["version_id"])
        await app.state.extensions.enable(world.owner, version_id)
        for invalid in ({}, {"limit": True}, {"limit": 201}, {"limit": 5, "path": "private"}):
            with pytest.raises(ValueError):
                await app.state.skills.start(world.owner, version_id, world.node_id, invalid, request_id=uuid4())
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT count(*) FROM tasks"))[0] == 0
            assert (await one(conn, "SELECT count(*) FROM skill_runs"))[0] == 0
        root = await app.state.skills.start(world.owner, version_id, world.node_id, {"limit": 5}, request_id=uuid4())
        assert root["input"] == {"limit": 5}
        from nexus.core.extensions.skills import _resolve
        assert _resolve(version["steps"][1]["arguments"], {}, root["input"]) == {"root_id": "workspace", "limit": 5}
    asyncio.run(run())


def test_repeated_run_returns_same_record_even_when_capacity_is_full(world):
    async def run():
        app = app_for(world, max_skill_runs=1)
        version = await run_approved(app, world, await generate(app, world, "skill", "概览"))
        version_id, request_id = UUID(version["version_id"]), uuid4()
        await app.state.extensions.enable(world.owner, version_id)
        async def start():
            return await app.state.skills.start(world.owner, version_id, world.node_id, {}, request_id=request_id)
        first, second = await asyncio.gather(start(), start())
        assert first["run_id"] == second["run_id"]
        with pytest.raises(Conflict):
            await app.state.skills.start(world.owner, version_id, world.node_id, {"different": 1}, request_id=request_id)
        with pytest.raises(AccessDenied, match="SKILL_STORAGE_FULL"):
            await app.state.skills.start(world.owner, version_id, world.node_id, {}, request_id=uuid4())
        await app.state.extensions.disable(world.owner, version_id)
        assert (await start())["run_id"] == first["run_id"]
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT count(*) FROM skill_runs"))[0] == 1
    asyncio.run(run())


def test_sandbox_journal_commits_before_send_and_caches_result(world):
    async def run():
        app = app_for(world, sandbox=True)
        controller = app.state.extensions.sandbox
        version = await generate(app, world, "tool", "统计", node_id=world.node_id)
        async with world.db.transaction(write=False) as conn:
            row = await one(conn, "SELECT * FROM extension_versions WHERE version_id=?", (version["version_id"],))
        execution = uuid4()
        class Transport(FakeSandbox):
            async def execute(self, *args, **kwargs):
                record = await controller.executions.get(execution)
                assert record["status"] == "sent"
                return await super().execute(*args, **kwargs)
        fake = Transport()
        controller.transport = fake
        value = {"entries": []}
        first = await controller._call(world.node_id, row, value, 10, execution_id=execution)
        second = await controller._call(world.node_id, row, value, 10, execution_id=execution)
        assert first == second and len(fake.calls) == 1
        with pytest.raises(Conflict):
            await controller._call(world.node_id, row, {"entries": [{"name": "x", "kind": "file"}]}, 10, execution_id=execution)
    asyncio.run(run())


def test_result_is_committed_before_ack_and_unknown_cannot_overwrite_it(world):
    async def run():
        app = app_for(world, sandbox=True)
        controller = app.state.extensions.sandbox
        version = await generate(app, world, "tool", "统计", node_id=world.node_id)
        record = await controller.executions.prepare(world.node_id, version, {"entries": []}, 10)
        execution = UUID(record["execution_id"])
        acknowledged = []
        class Transport:
            async def acknowledge(self, node, identity, digest):
                stored = await controller.executions.get(identity)
                assert stored["status"] == "settled"
                acknowledged.append(identity)
        controller.transport = Transport()
        result = SandboxResult(session_id=uuid4(), execution_id=execution, artifact_digest=version["artifact_digest"],
            outcome="failed", error_code="PROBE", stopped=True)
        await controller.receive_result(world.node_id, result)
        await controller.receive_result(world.node_id, result)
        assert acknowledged == [execution, execution]
        with pytest.raises(Conflict):
            await controller.executions.store(world.node_id, execution, version["artifact_digest"],
                {"outcome": "unknown", "error_code": "LOST", "stopped": False})
    asyncio.run(run())


def test_node_receipt_keeps_result_until_bound_ack_and_survives_restart(tmp_path):
    path = tmp_path / "sandbox.sqlite3"
    execution = uuid4()
    command = sandbox_command_digest(uuid4(), "a" * 64, "b" * 64, {}, 10)
    receipts = Receipts(path)
    try:
        assert receipts.accept(str(execution), "a" * 64, "c" * 64, command) is None
        receipts.finish(str(execution), "succeeded", '{"ok":true}', None)
        assert len(receipts.unacknowledged()) == 1
        with pytest.raises(ValueError):
            receipts.acknowledge(execution, "d" * 64)
    finally:
        receipts.close()
    receipts = Receipts(path)
    try:
        assert receipts.accept(str(execution), "a" * 64, "c" * 64, command)[3] == "succeeded"
        receipts.acknowledge(execution, "a" * 64)
        assert receipts.unacknowledged() == []
        assert receipts.get(execution)[4] == '{"ok":true}'
    finally:
        receipts.close()


@pytest.mark.parametrize("found", [True, False])
def test_reconciliation_queries_original_identity_without_reexecution(world, found):
    async def run():
        app = app_for(world, sandbox=True)
        controller = app.state.extensions.sandbox
        version = await generate(app, world, "tool", "统计", node_id=world.node_id)
        record = await controller.executions.prepare(world.node_id, version, {"entries": []}, 10)
        execution = UUID(record["execution_id"])
        await controller.executions.sent(execution)
        class Transport:
            connections = {}
            async def query(self, node, identity):
                assert identity == execution
                result = SandboxResult(session_id=uuid4(), execution_id=identity,
                    artifact_digest=version["artifact_digest"], outcome="failed", error_code="PROBE", stopped=True)
                return SandboxStatusResult(session_id=result.session_id, execution_id=identity,
                    status="settled" if found else "not_found",
                    command_digest=record["command_digest"] if found else None, result=result if found else None)
            async def acknowledge(self, node, identity, digest):
                assert (await controller.executions.get(identity))["status"] == "settled"
            async def execute(self, *args, **kwargs):
                pytest.fail("reconciliation must never replay execution")
        controller.transport = Transport()
        worker = asyncio.create_task(controller.reconcile_node(world.node_id))
        try:
            async with asyncio.timeout(3):
                while (await controller.executions.get(execution))["status"] != "settled":
                    await asyncio.sleep(.01)
            saved = await controller.executions.get(execution)
            assert saved["query_count"] == 1
            assert json.loads(saved["result_json"])["error_code"] == ("PROBE" if found else "SANDBOX_NOT_RECEIVED")
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())


def test_reconciliation_and_execution_storage_have_persistent_limits(world):
    async def run():
        app = app_for(world, sandbox=True, max_sandbox_executions=1)
        controller = app.state.extensions.sandbox
        version = await generate(app, world, "tool", "统计", node_id=world.node_id)
        record = await controller.executions.prepare(world.node_id, version, {"entries": []}, 10)
        execution = UUID(record["execution_id"])
        await controller.executions.sent(execution)
        for _ in range(64):
            assert await controller.executions.claim_query(execution)
        assert not await controller.executions.claim_query(execution)
        with pytest.raises(AccessDenied, match="SANDBOX_STORAGE_FULL"):
            await controller.executions.prepare(world.node_id, version, {"entries": []}, 10)
        assert (await controller.executions.get(execution))["query_count"] == 64
    asyncio.run(run())


def test_runtime_configuration_change_invalidates_tested_tool_approval(world):
    async def run():
        app = app_for(world, sandbox=True)
        app.state.extensions.sandbox.transport = FakeSandbox()
        version = await run_approved(app, world, await generate(app, world, "tool", "统计", node_id=world.node_id))
        policy = app.state.harness.link.config.sandbox_nodes[world.node_id]
        app.state.harness.link.config.sandbox_nodes[world.node_id] = policy.model_copy(update={"runsc_digest": "b" * 64})
        with pytest.raises(AccessDenied, match="SANDBOX_ENVIRONMENT_CHANGED"):
            await app.state.extensions.enable(world.owner, UUID(version["version_id"]))
        with pytest.raises(AccessDenied, match="SANDBOX_ENVIRONMENT_CHANGED"):
            await app.state.extensions.approve(world.owner, UUID(version["version_id"]), "approve",
                version["artifact_digest"], UUID(version["test"]["test_id"]))
    asyncio.run(run())


def test_cancel_stays_pending_while_sandbox_stop_is_unknown(world):
    async def run():
        app = app_for(world, sandbox=True)
        app.state.extensions.sandbox.transport = FakeSandbox()
        skill = await run_approved(app, world, await generate(app, world, "skill", "概览"))
        await app.state.extensions.enable(world.owner, UUID(skill["version_id"]))
        tool = await run_approved(app, world, await generate(app, world, "tool", "统计", node_id=world.node_id))
        root = await app.state.skills.start(world.owner, UUID(skill["version_id"]), world.node_id, {})
        execution = str(uuid4())
        now = timestamp(utc_now())
        async with world.db.transaction() as conn:
            await conn.execute("INSERT INTO extension_tool_runs(tool_run_id,owner_id,version_id,approval_id,node_id,skill_run_id,step_index,artifact_digest,environment_digest,input_json,input_digest,status,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?,'{}',?,'unknown',?,?)", (
                execution, str(world.owner_id), tool["version_id"], tool["approval"]["approval_id"], str(world.node_id),
                root["run_id"], tool["artifact_digest"], tool["environment_digest"], "d" * 64, now, now))
            await conn.execute("UPDATE skill_run_steps SET tool_run_id=?,status='unknown' WHERE run_id=? AND step_index=1", (execution, root["run_id"]))
        await app.state.skills.cancel(world.owner, UUID(root["run_id"]))
        await app.state.skills.tick()
        pending = await app.state.skills.get(world.owner, UUID(root["run_id"]))
        assert pending["status"] == "cancelling"
        assert pending["steps"][0]["tool_execution"]["status"] == "unknown"
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE extension_tool_runs SET status='cancelled' WHERE tool_run_id=?", (execution,))
        await app.state.skills.tick()
        assert (await app.state.skills.get(world.owner, UUID(root["run_id"])))["status"] == "cancelled"
    asyncio.run(run())


def test_late_durable_result_wins_over_old_disconnected_waiter(world):
    async def run():
        app = app_for(world, sandbox=True)
        controller = app.state.extensions.sandbox
        controller.transport = FakeSandbox()
        skill = await run_approved(app, world, await generate(app, world, "skill", "概览"))
        await app.state.extensions.enable(world.owner, UUID(skill["version_id"]))
        tool = await run_approved(app, world, await generate(app, world, "tool", "统计", node_id=world.node_id))
        await app.state.extensions.enable(world.owner, UUID(tool["version_id"]))
        root = await app.state.skills.start(world.owner, UUID(skill["version_id"]), world.node_id, {})
        async with world.db.transaction(write=False) as conn:
            version = await one(conn, "SELECT * FROM extension_versions WHERE version_id=?", (tool["version_id"],))
        async def disconnected(node, version, value, timeout, *, execution_id, **kwargs):
            await controller.executions.prepare(node, version, value, timeout, execution_id=execution_id)
            await controller.executions.sent(execution_id)
            await controller.executions.store(node, execution_id, version["artifact_digest"],
                {"outcome": "failed", "error_code": "EXECUTION_RESULT_UNKNOWN", "stopped": True})
            return {"outcome": "unknown", "error_code": "OLD_CONNECTION_LOST", "stopped": False}
        controller._call = disconnected
        result = await controller.execute_skill_step(owner_id=str(world.owner_id), version=version,
            input_value={"entries": []}, skill_run_id=root["run_id"], step_index=1)
        assert result == "failed"
        async with world.db.transaction(write=False) as conn:
            record = await one(conn, "SELECT status,error_json FROM extension_tool_runs WHERE skill_run_id=?", (root["run_id"],))
            assert record["status"] == "failed"
            assert json.loads(record["error_json"])["code"] == "EXECUTION_RESULT_UNKNOWN"
    asyncio.run(run())


def test_bounded_negative_candidate_case_is_checked_as_expected_rejection(world, monkeypatch):
    candidate = json.loads(fixture_generation("tool", "stats", parent_version_id=None, tool_version_id=None))
    candidate["tests"].append({"name": "unknown kind", "input": {"entries": [{"name": "x", "kind": "socket"}]}, "expect_error": True})
    monkeypatch.setattr(service_module, "fixture_generation", lambda *a, **k: json.dumps(candidate))
    async def run():
        app = app_for(world, sandbox=True)
        app.state.extensions.sandbox.transport = FakeSandbox()
        version = await generate(app, world, "tool", "统计", node_id=world.node_id)
        assert version["test_status"] == "passed"
        assert version["approval_status"] == "none" and not version["enabled"]
    asyncio.run(run())
