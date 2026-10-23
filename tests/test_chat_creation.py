"""Conversation capability creation: persisted evidence, no nested model or host effects."""

import asyncio
import copy
import json
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic import SecretStr

from nexus.contracts.chat import ChatSubmission, ConversationCreate, ChatResume
from nexus.contracts.work import SkillCreateArguments, WorkGrantCreate
from nexus.core.cognitive import work_catalog
from nexus.core.extensions.fixtures import _skill_v1
from nexus.core.models.chat import DeepSeekChatProvider, build_request
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from test_chat import Stream, sse
from test_chat_skills import app_for, prepare
from test_chat_work import WorkProvider
from test_plugin_lifecycle import candidate, full


def skill_action(native=False):
    if native:
        from test_native_skills import generation
    return {"intent":"skill_create", "arguments":{"reason":"当前任务需要可复用的固定流程，现有目录没有对应 Skill。",
        "skill":generation().model_dump(mode="json") if native else copy.deepcopy(_skill_v1())}}


async def start(app, world, actions, *, text="创建一个工作区概览 Skill", profile="standard", plugins=False, mode="normal", workspace=False):
    chat = app.state.chat
    chat.skills.extensions.config.generation_enabled = True
    chat.plugins.config.enabled = plugins
    if plugins:
        await chat.plugins.seed()
    if mode == "full":
        await full(chat, world.owner)
    chat.provider = WorkProvider(actions)
    conv = await chat.create(world.owner, ConversationCreate(request_id=uuid4()))
    grant = await chat.work.grant(world.owner, conv["conversation_id"], WorkGrantCreate(
        request_id=uuid4(), node_id=world.node_id, consent=True, profile=profile))
    async with world.db.transaction(write=False) as conn:
        catalog = await work_catalog.catalog(chat, conn, world.owner, world.node_id)
    turn = await chat.submit(world.owner, conv["conversation_id"], ChatSubmission(
        request_id=uuid4(), expected_revision=conv["revision"], text=text, allow_model=True,
        allow_tools=True, node_id=world.node_id, work_grant_id=None if workspace else UUID(grant["grant_id"]), workspace_work=workspace,
        work_digest=catalog["work_digest"], allow_work_results=True))
    return turn, grant, catalog["catalog"]


@pytest.mark.parametrize("text", ["创建一个工作区概览 Skill", "把工作区检查整理为今后可复用的流程，缺少能力时自行补齐"])
@pytest.mark.parametrize("native,mode", [(False,"normal"),("skill_native_steps_v1" in json.dumps(SkillCreateArguments.model_json_schema()),"full")])
def test_chat_creates_real_skill_candidate_and_retains_version_approval(world, monkeypatch, text, native, mode):
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        turn, grant, catalog = await start(app, world, [skill_action(native)], text=text, plugins=native, mode=mode)
        for _ in range(3):
            await chat.process_one()
        current = await chat.get(world.owner, turn["turn_id"])
        result = current["work_steps"][0]["result"]
        assert result["status"] == "succeeded", current
        assert result["verification"] == "skill_contract_standins_only"
        data = result["data"]
        assert data["created"] and data["approval_required"] and not data["enabled"]
        assert data["execution"] == "not_run" and data["test_status"] == "passed"
        saved = await app.state.extensions.get_version(world.owner, UUID(data["version_id"]))
        assert saved["markdown"] and saved["steps"] and saved["approval_status"] == "none"
        with pytest.raises(AccessDenied):
            await app.state.skills.start(world.owner, UUID(data["version_id"]), world.node_id, {})
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT count(*) FROM extension_versions"))[0] == 1
            assert (await one(conn, "SELECT count(*) FROM extension_tests"))[0] == 1
            assert (await one(conn, "SELECT count(*) FROM tasks"))[0] == 0
            assert (await one(conn, "SELECT count(*) FROM skill_runs"))[0] == 0
            assert (await one(conn, "SELECT used_operations FROM chat_work_grants WHERE grant_id=?", (grant["grant_id"],)))[0] == 1
            calls = await conn.execute_fetchall("SELECT work_root_turn_id,extension_request_id FROM model_calls")
            assert len(calls) == 2 and all(c[0] == turn["turn_id"] and c[1] is None for c in calls)
        system = build_request(chat.config, [{"role":"user","content":text}], work_catalog=catalog, allow_tools=True)["messages"][0]["content"]
        assert "existing tools cannot meet it" in system and "one-off task" in system
        assert "No arbitrary shell, installation or tool generation" not in system
        assert chat.provider.calls[1][0][-1]["tool_call_id"] == "work-call-1"
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["skill", "plugin"])
@pytest.mark.parametrize("restriction", ["read", "plan", "revoked", "cancelled", "recovery", "disabled", "operations"])
def test_creation_rechecks_authority_before_persistence(world, monkeypatch, restriction, kind):
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        action = skill_action() if kind == "skill" else {"intent":"plugin_install", "arguments":{"release":candidate().model_dump(mode="json")}}
        turn, grant, _ = await start(app, world, [action], plugins=kind=="plugin", profile="read" if restriction == "read" else "standard")
        await chat.process_one()  # proposal saved; no candidate yet
        async with world.db.transaction() as conn:
            if restriction == "plan":
                await conn.execute("UPDATE conversations SET plan_mode=1 WHERE conversation_id=?", (turn["conversation_id"],))
            elif restriction == "revoked":
                await conn.execute("UPDATE chat_work_grants SET revoked_at=? WHERE grant_id=?", (timestamp(utc_now()),grant["grant_id"]))
            elif restriction == "cancelled":
                await chat.work.abort(conn, turn["turn_id"], "WORK_STOPPED", utc_now())
            elif restriction == "recovery":
                await conn.execute("UPDATE chat_turns SET recovery_required=1 WHERE turn_id=?", (turn["turn_id"],))
            elif restriction == "operations":
                await conn.execute("UPDATE chat_work_grants SET used_operations=max_operations WHERE grant_id=?", (grant["grant_id"],))
        if restriction == "disabled":
            app.state.extensions.config.generation_enabled = False
            chat.plugins.config.enabled = False
        await chat.work.advance()
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT count(*) FROM extension_versions"))[0] == 0
            assert (await one(conn, "SELECT count(*) FROM extension_requests"))[0] == 0
            assert (await one(conn, "SELECT count(*) FROM plugin_versions WHERE builtin=0"))[0] == 0
    asyncio.run(run())


def test_skill_dedup_and_failed_test_do_not_overwrite_or_enable(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        actions = [skill_action(), skill_action()]
        changed = copy.deepcopy(actions[0]); changed["arguments"]["skill"]["markdown"] += " changed"
        actions.append(changed)
        turn, _, _ = await start(app, world, actions)
        for _ in range(5): await app.state.chat.process_one()
        current = await app.state.chat.get(world.owner, turn["turn_id"])
        assert current["work_steps"][0]["result"]["data"]["version_id"] == current["work_steps"][1]["result"]["data"]["version_id"]
        assert current["work_steps"][2]["result"]["status"] == "failed"
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT count(*) FROM extension_versions"))[0] == 1
            assert (await one(conn, "SELECT count(*) FROM extension_tests"))[0] == 1
        broken = skill_action()
        broken["arguments"]["skill"]["manifest"]["capability_id"] = "user.broken_overview"
        broken["arguments"]["skill"]["manifest"]["output_schema"]["properties"]["system"]["properties"]["os_name"]["const"] = "impossible"
        other, _, _ = await start(app, world, [broken])
        for _ in range(3): await app.state.chat.process_one()
        failed = (await app.state.chat.get(world.owner, other["turn_id"]))["work_steps"][0]["result"]
        assert failed["status"] == "failed" and failed["data"]["created"]
        assert failed["data"]["test_status"] == "failed" and not failed["data"]["enabled"]
        assert failed["data"]["approval_required"] and not failed["data"]["approval_available"]
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["normal","full"])
def test_plugin_creation_tests_automatically_and_respects_enable_policy(world, monkeypatch, mode):
    app = app_for(world, monkeypatch, sandbox=True)
    async def run():
        chat = app.state.chat
        turn, _, _ = await start(app, world, [{"intent":"plugin_install","arguments":{"release":candidate().model_dump(mode="json")}}], plugins=True, mode=mode, text="创建一个整数加法插件")
        tester = chat.plugins.tester
        tester.transport.status = lambda _: {"available":True}
        async def execute(*args, **kwargs): return {"outcome":"succeeded","output":{"sum":5}}
        tester.transport.execute = execute  # protocol simulation, not real runsc
        await chat.process_one(); await chat.process_one()
        async with world.db.transaction(write=False) as conn:
            pending = await one(conn, "SELECT * FROM plugin_test_runs")
            assert pending is not None
        await tester.process(pending, asyncio.Event())
        for _ in range(3): await chat.process_one()
        current = await chat.get(world.owner, turn["turn_id"])
        result = current["work_steps"][0]["result"]
        assert result["status"] == "succeeded", current
        assert bool(result["data"]["enabled"]) == (mode == "full")
        assert bool(result["data"]["approval_required"]) == (mode == "normal")
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT count(*) FROM plugin_install_requests"))[0] == 1
            assert (await one(conn, "SELECT count(*) FROM chat_work_operations"))[0] == 1
            assert (await one(conn, "SELECT count(*) FROM tasks"))[0] == 0
    asyncio.run(run())


def test_creation_catalog_and_native_provider_parser(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        turn, _, catalog = await start(app, world, [], plugins=True)
        entry = next(item for item in catalog if item["name"] == "skill_create")
        args = skill_action()["arguments"]
        chunks = [sse(delta={"tool_calls":[{"index":0,"id":"create-skill","type":"function","function":{"name":"skill_create","arguments":json.dumps(args)}}]}),
                  sse(finish="tool_calls",usage={"prompt_tokens":100,"completion_tokens":200}),b"data: [DONE]\n\n"]
        async def handle(request):
            assert len(request.content) <= 32768
            return httpx2.Response(200,headers={"content-type":"text/event-stream"},stream=Stream(chunks))
        provider = DeepSeekChatProvider(SecretStr("synthetic-no-network"), app.state.chat.config, transport=httpx2.MockTransport(handle))
        async def receive(part): pass
        reply = await provider.invoke([{"role":"user","content":"创建 Skill"}], receive, work_catalog=catalog,allow_tools=True)
        assert reply.proposal["intent"] == "skill_create" and not reply.validation_errors
        SkillCreateArguments.model_validate_json(json.dumps(reply.proposal["arguments"]))
        app.state.extensions.config.generation_enabled = False
        async with world.db.transaction(write=False) as conn:
            hidden = await work_catalog.catalog(app.state.chat, conn, world.owner, world.node_id)
        assert "skill_create" not in {item["name"] for item in hidden["catalog"]}
        assert entry["parameters"]["additionalProperties"] is False
    asyncio.run(run())


def test_existing_skill_stays_discoverable_with_creation_and_plugins(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        version = await prepare(app, world)
        turn, _, catalog = await start(app, world, [], plugins=True, workspace=True)
        assert any(item.get("version_id") == version["version_id"] for item in catalog)
        for _ in range(2): await app.state.chat.process_one()
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn,"SELECT count(*) FROM extension_versions"))[0] == 1
        assert (await app.state.chat.get(world.owner, turn["turn_id"]))["work_steps"] == []
    asyncio.run(run())


@pytest.mark.parametrize("stop", ["cancel", "restart"])
def test_stop_after_skill_persistence_keeps_candidate_and_resume_is_idempotent(world, monkeypatch, stop):
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        turn, _, _ = await start(app, world, [skill_action()], workspace=True)
        await chat.process_one()
        original = app.state.extensions.test_version
        async def interrupt(principal, version_id, **kwargs):
            if stop == "cancel":
                async with world.db.transaction() as conn:
                    await chat.work.abort(conn, turn["turn_id"], "WORK_STOPPED", utc_now())
                return await original(principal, version_id, **kwargs)
            raise asyncio.CancelledError()
        monkeypatch.setattr(app.state.extensions, "test_version", interrupt)
        if stop == "restart":
            with pytest.raises(asyncio.CancelledError): await chat.work.advance()
            await chat.recover()
        else:
            await chat.work.advance()
        async with world.db.transaction(write=False) as conn:
            row = await one(conn, "SELECT * FROM extension_versions")
            assert row["test_status"] == "untested" and not row["enabled"]
            identifier = row["version_id"]
            assert (await one(conn, "SELECT count(*) FROM extension_tests"))[0] == 0
        await chat.work.advance()
        assert len(chat.provider.calls) == 1
        if stop == "restart":
            monkeypatch.setattr(app.state.extensions, "test_version", original)
            await chat.journal.resume(world.owner, turn["turn_id"], ChatResume(request_id=uuid4(),consent=True))
            for _ in range(2): await chat.process_one()
            result = (await chat.get(world.owner, turn["turn_id"]))["work_steps"][0]["result"]
            assert result["data"]["version_id"] == identifier and result["status"] == "succeeded"
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT count(*) FROM extension_versions"))[0] == 1
            assert (await one(conn, "SELECT count(*) FROM chat_work_operations"))[0] == 1
            assert (await one(conn, "SELECT count(*) FROM tasks"))[0] == 0
    asyncio.run(run())


def test_plan_mode_during_plugin_testing_prevents_full_auto_enable(world, monkeypatch):
    app = app_for(world, monkeypatch, sandbox=True)
    async def run():
        chat = app.state.chat
        turn, _, _ = await start(app, world, [{"intent":"plugin_install", "arguments":{"release":candidate().model_dump(mode="json")}}],plugins=True,mode="full")
        tester = chat.plugins.tester
        tester.transport.status = lambda _: {"available":True}
        async def execute(*args, **kwargs):
            async with world.db.transaction() as conn:
                await conn.execute("UPDATE conversations SET plan_mode=1 WHERE conversation_id=?",(turn["conversation_id"],))
            return {"outcome":"succeeded","output":{"sum":5}}
        tester.transport.execute = execute
        await chat.process_one(); await chat.work.advance()
        async with world.db.transaction(write=False) as conn:
            runrow = await one(conn, "SELECT * FROM plugin_test_runs")
        await tester.process(runrow, asyncio.Event())
        async with world.db.transaction(write=False) as conn:
            row = await one(conn, "SELECT * FROM plugin_versions WHERE builtin=0")
            assert not row["enabled"] and not row["approved"]
    asyncio.run(run())
