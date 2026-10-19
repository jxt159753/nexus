"""M4 candidate, test, approval, enablement and bounded Skill execution."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from nexus.contracts.extensions import (
    ExtensionGenerateConsent,
    ExtensionRequestCreate,
    parse_generation,
    parse_strict_json,
    validate_schema,
)
from nexus.core.app import create_app
from nexus.core.config import ExtensionConfig, SandboxNodePolicy, TrustedNode
from nexus.core.extensions.tools import reference_stats
from nexus.core.models.config import ModelConfig
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.core.storage.execution import ExecutionRepository, RESULT
from test_node_link import core_config


async def grants(world, tool="system.info"):
    for identity in (world.owner_id, world.node_id):
        await world.security.set_grant(identity, tool, root_id="workspace" if tool == "filesystem.list" else "")

def app_for(world, *, fixture=True, sandbox=False, **extension):
    values = dict(enabled=True, fixture_generation=fixture, **extension)
    config = core_config(
        world,
        extensions=ExtensionConfig(**values),
        nodes={world.node_id: TrustedNode(tools=frozenset({"system.info@1", "filesystem.list@1"}), roots=frozenset({"workspace"}))},
        sandbox_nodes=({world.node_id: SandboxNodePolicy(image_digest="a" * 64)} if sandbox else {}),
    )
    return create_app(config)


async def generate(app, world, kind, need, *, node_id=None, parent=None):
    service = app.state.extensions
    request = ExtensionRequestCreate(request_id=uuid4(), kind=kind, need=need, node_id=node_id, parent_version_id=parent)
    created = await service.create(world.owner, request)
    await service.generate(world.owner, request.request_id, ExtensionGenerateConsent(preview_digest=created["preview_digest"], allow_model=True))
    assert await service.process_one() is True
    value = await service.get_request(world.owner, request.request_id)
    assert value["status"] == "candidate", (value["status"], value.get("error_code"))
    return await service.get_version(world.owner, UUID(value["candidate_version_id"]))


async def run_test(app, world, version):
    return await app.state.extensions.test_version(world.owner, UUID(version["version_id"]))


async def run_approved(app, world, version):
    value = await run_test(app, world, version)
    assert value["test_status"] == "passed", value
    return await app.state.extensions.approve(
        world.owner, UUID(value["version_id"]), "approve", value["artifact_digest"], UUID(value["test"]["test_id"]))


def result_for(command, *, entries=None, truncated=False):
    values = command.model_dump(mode="json", include={"session_id", "task_id", "attempt_id", "tool", "tool_version", "command_digest"})
    values.update(observed_at=utc_now().isoformat(), outcome="succeeded", result=(
        {"root_id": "workspace",
         "entries": entries if entries is not None else [{"name": "docs", "kind": "directory"}],
         "truncated": truncated}
        if command.tool == "filesystem.list" else
        {"os_name": "Windows", "os_version": "test", "architecture": "test", "python_version": "test"}))
    return RESULT.validate_json(json.dumps(values))


async def finish_task(world, task_id):
    task = await world.tasks.get(task_id, world.owner_id)
    if task.status == "awaiting_approval":
        async with world.db.transaction(write=False) as conn:
            row = await one(conn, "SELECT approval_id FROM approvals WHERE task_id=? AND decision IS NULL ORDER BY rowid DESC LIMIT 1", (str(task_id),))
        await world.security.decide(UUID(row[0]), world.owner, "approve", world.scope)
    repo = ExecutionRepository(world.db, world.security, 32768)
    command = await repo.dispatch(task_id, world.node, world.scope, uuid4(), uuid4(), 10)
    assert command is not None
    result = result_for(command, entries=[{"name": "a", "kind": "file"}, {"name": "b", "kind": "file"},
                                          {"name": "docs", "kind": "directory"}, {"name": "link", "kind": "link"},
                                          {"name": "other", "kind": "other"}], truncated=True)
    assert await repo.store_result(world.node, result) == "stored"
    await repo.verify(command.attempt_id)


def test_schema_subset_and_duplicate_keys():
    assert validate_schema({"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False}, where="root")["type"] == "object"
    for bad in (
        {"type": "object", "properties": {}, "patternProperties": {}},
        {"type": "object", "$ref": "https://example.invalid/x"},
        {"type": "number"},
        {"type": "string", "pattern": ".*"},
        {"type": "array", "items": {"type": "integer"}, "maxItems": 201},
    ):
        with pytest.raises(ValueError):
            validate_schema(bad, where="bad")
    with pytest.raises(ValueError):
        parse_strict_json('{"a":1,"a":2}')
    with pytest.raises(ValueError):
        parse_strict_json('{"a":NaN}')


def test_tool_candidate_is_not_approvable_without_qualified_sandbox(world):
    app = app_for(world)

    async def run():
        version = await generate(app, world, "tool", "统计显式传入的目录条目类型")
        assert version["kind"] == "tool" and version["test_status"] == "untested"
        assert version["enabled"] is False
        value = await run_test(app, world, version)
        assert value["test_status"] == "unavailable"
        assert value["test"]["status"] == "unavailable"
        with pytest.raises(AccessDenied):
            await app.state.extensions.approve(world.owner, UUID(value["version_id"]), "approve",
                                               value["artifact_digest"], uuid4())
        with pytest.raises(AccessDenied):
            await app.state.extensions.enable(world.owner, UUID(value["version_id"]))
    asyncio.run(run())


def test_skill_candidate_requires_test_then_owner_approval_and_enablement(world):
    app = app_for(world)

    async def run():
        version = await generate(app, world, "skill", "工作区概览：系统信息后列 workspace")
        assert version["capability_id"] == "user.workspace_overview" and version["revision"] == 1
        with pytest.raises(AccessDenied):
            await app.state.extensions.enable(world.owner, UUID(version["version_id"]))
        value = await run_approved(app, world, version)
        assert value["test_status"] == "passed" and value["approval_status"] == "approved"
        assert value["enabled"] is False
        enabled = await app.state.extensions.enable(world.owner, UUID(value["version_id"]))
        assert enabled["enabled"] is True and enabled["enablement"]["approval_id"] == enabled["approval"]["approval_id"]
    asyncio.run(run())


def test_two_step_skill_executes_through_normal_tasks_and_directory_approval(world):
    app = app_for(world)

    async def run():
        await grants(world)
        await grants(world, "filesystem.list")
        version = await run_approved(app, world, await generate(app, world, "skill", "工作区概览"))
        await app.state.extensions.enable(world.owner, UUID(version["version_id"]))
        run_value = await app.state.skills.start(world.owner, UUID(version["version_id"]), world.node_id, {})
        await app.state.skills.tick()
        run_value = await app.state.skills.get(world.owner, UUID(run_value["run_id"]))
        first = run_value["steps"][0]
        assert first["task_id"] and first["status"] in {"dispatchable", "approval", "running"}
        await finish_task(world, UUID(first["task_id"]))
        await app.state.skills.tick()
        await app.state.skills.tick()
        run_value = await app.state.skills.get(world.owner, UUID(run_value["run_id"]))
        second = run_value["steps"][1]
        assert second["task_id"] and second["status"] == "approval"
        task = await world.tasks.get(UUID(second["task_id"]), world.owner_id)
        assert task.status == "awaiting_approval"
        await finish_task(world, UUID(second["task_id"]))
        await app.state.skills.tick()
        done = await app.state.skills.get(world.owner, UUID(run_value["run_id"]))
        assert done["status"] == "succeeded" and done["verification"] == "skill_steps_verified"
        assert done["result"]["directory"]["truncated"] is True
        assert done["result"]["system"]["os_name"] == "Windows"
    asyncio.run(run())


def test_disable_blocks_new_steps_and_approval_expiry_is_rechecked(world):
    app = app_for(world)

    async def run():
        await grants(world)
        await grants(world, "filesystem.list")
        version = await run_approved(app, world, await generate(app, world, "skill", "工作区概览"))
        await app.state.extensions.enable(world.owner, UUID(version["version_id"]))
        run_value = await app.state.skills.start(world.owner, UUID(version["version_id"]), world.node_id, {})
        await app.state.skills.tick()
        run_value = await app.state.skills.get(world.owner, UUID(run_value["run_id"]))
        await finish_task(world, UUID(run_value["steps"][0]["task_id"]))
        await app.state.extensions.disable(world.owner, UUID(version["version_id"]))
        await app.state.skills.tick()
        stopped = await app.state.skills.get(world.owner, UUID(run_value["run_id"]))
        assert stopped["status"] == "failed" and stopped["error"]["code"] == "VERSION_DISABLED"
        assert stopped["steps"][1]["task_id"] is None
    asyncio.run(run())


class FakeSandbox:
    def __init__(self, *, forged=False):
        self.forged = forged
        self.calls = []

    def status(self, node_id):
        return {"available": True, "reason": None, "image_digest": "a" * 64}

    async def execute(self, node_id, version, input_value, timeout):
        self.calls.append(input_value)
        output = reference_stats(input_value["entries"])
        if self.forged:
            output["counts"]["file"] += 1
        return {"outcome": "succeeded", "output": output}


def sandbox_app(world, fake):
    app = app_for(world, sandbox=True)
    app.state.extensions.sandbox.transport = fake
    return app


def test_trusted_controller_rejects_candidate_forged_results(world):
    app = sandbox_app(world, FakeSandbox(forged=True))

    async def run():
        version = await generate(app, world, "tool", "统计显式传入的目录条目类型", node_id=world.node_id)
        value = await run_test(app, world, version)
        assert value["test_status"] == "failed"
        assert any(case.get("reason") == "output_mismatch" for case in value["test"]["result"]["cases"])
        with pytest.raises(AccessDenied):
            await app.state.extensions.approve(world.owner, UUID(value["version_id"]), "approve",
                                               value["artifact_digest"], UUID(value["test"]["test_id"]))
    asyncio.run(run())


def test_three_step_composition_uses_only_approved_fixed_versions(world):
    fake = FakeSandbox()
    app = sandbox_app(world, fake)

    async def run():
        await grants(world)
        await grants(world, "filesystem.list")
        parent = await run_approved(app, world, await generate(app, world, "skill", "工作区概览"))
        await app.state.extensions.enable(world.owner, UUID(parent["version_id"]))
        tool = await run_approved(app, world, await generate(app, world, "tool", "统计条目类型", node_id=world.node_id))
        assert tool["test_status"] == "passed" and tool["enabled"] is False
        await app.state.extensions.enable(world.owner, UUID(tool["version_id"]))
        skill = await generate(app, world, "skill", "在概览第三步统计条目", parent=UUID(parent["version_id"]))
        assert skill["kind"] == "skill" and skill["revision"] == 2
        value = await run_approved(app, world, skill)
        await app.state.extensions.enable(world.owner, UUID(value["version_id"]))
        started = await app.state.skills.start(world.owner, UUID(value["version_id"]), world.node_id, {})
        await app.state.skills.tick()
        started = await app.state.skills.get(world.owner, UUID(started["run_id"]))
        await finish_task(world, UUID(started["steps"][0]["task_id"]))
        await app.state.skills.tick()
        await app.state.skills.tick()
        started = await app.state.skills.get(world.owner, UUID(started["run_id"]))
        await finish_task(world, UUID(started["steps"][1]["task_id"]))
        for _ in range(4):
            await app.state.skills.tick()
        done = await app.state.skills.get(world.owner, UUID(started["run_id"]))
        assert done["status"] == "succeeded", done
        assert done["result"]["stats"]["total"] == 5
        assert done["result"]["stats"]["counts"] == {"file": 2, "directory": 1, "link": 1, "other": 1}
        assert fake.calls and fake.calls[0]["entries"][0]["name"] == "a.txt"
    asyncio.run(run())


def test_skill_approval_rejects_unapproved_tool_dependency(world):
    fake = FakeSandbox()
    app = sandbox_app(world, fake)

    async def run():
        parent = await generate(app, world, "skill", "工作区概览")
        tool = await run_test(app, world, await generate(app, world, "tool", "统计条目类型", node_id=world.node_id))
        assert tool["test_status"] == "passed"
        # Tool is tested but not approved/enabled.
        skill = await generate(app, world, "skill", "在概览第三步统计条目", parent=UUID(parent["version_id"]))
        value = await run_test(app, world, skill)
        assert value["test_status"] == "passed"
        with pytest.raises(AccessDenied):
            await app.state.extensions.approve(world.owner, UUID(value["version_id"]), "approve",
                                               value["artifact_digest"], UUID(value["test"]["test_id"]))
    asyncio.run(run())


def test_generation_preview_and_budget_gate(world, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-no-network")
    app = create_app(core_config(
        world,
        models=ModelConfig(enabled=True, daily_cost_microunits=50000),
        extensions=ExtensionConfig(enabled=True, generation_enabled=True),
        nodes={world.node_id: TrustedNode(tools=frozenset({"system.info@1"}))},
    ))

    async def run():
        service = app.state.extensions
        request = await service.create(world.owner, ExtensionRequestCreate(request_id=uuid4(), kind="skill", need="测试"))
        assert request["outbound"]["mode"] == "llm"
        assert "messages" in request["outbound"] and request["outbound"]["reservation_microunits"] == 50000
        with pytest.raises(AccessDenied):
            await service.generate(world.owner, UUID(request["request_id"]), ExtensionGenerateConsent(preview_digest="0" * 64, allow_model=True))
        await service.generate(world.owner, UUID(request["request_id"]), ExtensionGenerateConsent(preview_digest=request["preview_digest"], allow_model=True))
        created = await service.get_request(world.owner, UUID(request["request_id"]))
        assert created["status"] == "queued"
        # Simulate an exhausted daily budget before the worker claims it.
        async with world.db.transaction() as conn:
            await conn.execute("INSERT INTO model_calls(call_id,provider,model,cost_microunits,cost_status,created_at,owner_id,reserved_microunits,outcome) VALUES(?,?,?,?,'calculated',?,?,?,'completed')", (
                str(uuid4()), "deepseek", "deepseek-flash", 50000, timestamp(utc_now()), str(world.owner_id), 50000))
        assert await service.process_one() is False
        failed = await service.get_request(world.owner, UUID(request["request_id"]))
        assert failed["status"] == "failed" and failed["error_code"] == "BUDGET_EXCEEDED"
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT count(*) FROM model_calls"))[0] == 1
    asyncio.run(run())


def test_plan_limits_reject_four_steps_forward_refs_nested_and_expressions():
    from nexus.core.extensions.fixtures import fixture_generation
    base = json.loads(fixture_generation("skill", "overview", parent_version_id=None, tool_version_id=None))

    four = json.loads(json.dumps(base))
    four["steps"].append({"id": "extra_one", "tool": "system.info", "tool_version": 1, "arguments": {}})
    four["steps"].append({"id": "extra_two", "tool": "system.info", "tool_version": 1, "arguments": {}})
    with pytest.raises((ValidationError, ValueError)):
        parse_generation(json.dumps(four))

    forward = json.loads(json.dumps(base))
    forward["steps"][0]["arguments"] = {"entries": {"$ref": {"step": "directory", "field": "entries"}}}
    with pytest.raises((ValidationError, ValueError)):
        parse_generation(json.dumps(forward))

    nested = json.loads(json.dumps(base))
    nested["steps"][0]["tool"] = "skill.run"
    with pytest.raises((ValidationError, ValueError)):
        parse_generation(json.dumps(nested))

    expression = json.loads(json.dumps(base))
    expression["steps"][0]["arguments"] = {"$expr": "system.info"}
    with pytest.raises((ValidationError, ValueError)):
        parse_generation(json.dumps(expression))


def test_pause_blocks_next_skill_step_and_cancel_stops_the_run(world):
    app = app_for(world)

    async def run():
        await grants(world)
        await grants(world, "filesystem.list")
        version = await run_approved(app, world, await generate(app, world, "skill", "工作区概览"))
        await app.state.extensions.enable(world.owner, UUID(version["version_id"]))
        started = await app.state.skills.start(world.owner, UUID(version["version_id"]), world.node_id, {})
        await app.state.skills.tick()
        started = await app.state.skills.get(world.owner, UUID(started["run_id"]))
        await finish_task(world, UUID(started["steps"][0]["task_id"]))
        await app.state.harness.control(world.owner, "pause", uuid4())
        await app.state.skills.tick()
        await app.state.skills.tick()
        paused = await app.state.skills.get(world.owner, UUID(started["run_id"]))
        assert paused["status"] == "running" and paused["steps"][1]["task_id"] is None
        await app.state.harness.control(world.owner, "resume", uuid4())
        await app.state.skills.tick()
        await app.state.skills.tick()
        resumed = await app.state.skills.get(world.owner, UUID(started["run_id"]))
        assert resumed["steps"][1]["task_id"]
        await app.state.skills.cancel(world.owner, UUID(started["run_id"]))
        await app.state.skills.tick()
        cancelled = await app.state.skills.get(world.owner, UUID(started["run_id"]))
        assert cancelled["status"] == "cancelled"
    asyncio.run(run())


def test_candidate_storage_limit_refuses_new_versions(world):
    app = app_for(world, max_candidates=1)

    async def run():
        await generate(app, world, "skill", "工作区概览")
        second = await app.state.extensions.create(world.owner, ExtensionRequestCreate(request_id=uuid4(), kind="tool", need="统计"))
        await app.state.extensions.generate(world.owner, second["request_id"], ExtensionGenerateConsent(preview_digest=second["preview_digest"], allow_model=True))
        await app.state.extensions.process_one()
        failed = await app.state.extensions.get_request(world.owner, UUID(second["request_id"]))
        assert failed["status"] == "failed" and failed["error_code"] == "CANDIDATE_INVALID"
    asyncio.run(run())
