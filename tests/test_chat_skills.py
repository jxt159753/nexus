"""M5 consent, fixed plans, shared execution, recovery and result boundaries."""

import asyncio
import json
import sqlite3
from uuid import UUID, uuid4

import pytest

from nexus.contracts.chat import ChatActionDecision, ChatPlanPreview, ChatResultConsent, ChatResultPreview, ChatSubmission, ConversationCreate, ConversationUpdate
from nexus.core.app import create_app
from nexus.core.config import ExtensionConfig, SandboxNodePolicy, TrustedNode
from nexus.core.models.config import ModelConfig
from nexus.core.models.providers import ModelReply
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict
from test_chat import rows
from test_extensions_m4 import FakeSandbox, generate, run_approved, grants, finish_task
from test_node_link import core_config


class SkillProvider:
    def __init__(self):
        self.calls = []
        self.proposal = None

    async def invoke(self, messages, receive, **options):
        self.calls.append((messages, options))
        if options.get("final_result"):
            await receive("根据实际结果，目录仅统计已返回的条目。")
            return ModelReply("根据实际结果，目录仅统计已返回的条目。", 100, 20, True)
        proposal = self.proposal
        if proposal is None:
            proposal = {"intent": "run_skill", "arguments": {"version_id": options["skill_catalog"][0]["version_id"], "input": {}}}
        native = {"id": "actual-skill-call", "type": "function", "function": {"name": proposal["intent"], "arguments": json.dumps(proposal["arguments"])}}
        return ModelReply("已准备流程，请核对步骤和参数。", 100, 20, True, proposal=proposal, native_tool_call=native)


def app_for(world, monkeypatch, *, sandbox=False):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-m5-no-network")
    app = create_app(core_config(world, models=ModelConfig(enabled=True, chat_enabled=True),
        extensions=ExtensionConfig(enabled=True, fixture_generation=True),
        sandbox_nodes={world.node_id: SandboxNodePolicy(image_digest="a"*64)} if sandbox else {},
        nodes={world.node_id: TrustedNode(tools=frozenset({"system.info@1", "filesystem.list@1"}), roots=frozenset({"workspace"}))}))
    app.state.chat.provider = SkillProvider()
    return app


async def prepare(app, world):
    await grants(world)
    await grants(world, "filesystem.list")
    version = await run_approved(app, world, await generate(app, world, "skill", "工作区概览"))
    await app.state.extensions.enable(world.owner, UUID(version["version_id"]))
    return version


async def suggest(app, world, *, conversation=None, **changes):
    chat = app.state.chat
    conversation = conversation or await chat.create(world.owner, ConversationCreate(request_id=uuid4()))
    directory = await chat.skill_catalog(world.owner, world.node_id)
    history = await chat.history(world.owner, conversation["conversation_id"])
    submission = ChatSubmission(**{**dict(request_id=uuid4(), expected_revision=history["conversation"]["revision"],
        text="给我做一份工作区概览", allow_model=True, allow_tools=True, node_id=world.node_id,
        allow_skills=True, skills_digest=directory["skills_digest"]), **changes})
    turn = await chat.submit(world.owner, conversation["conversation_id"], submission)
    await chat.process_one()
    return conversation, await chat.get(world.owner, turn["turn_id"]), submission


def confirm(turn):
    return ChatActionDecision(proposal_digest=turn["proposal_digest"], decision="confirm")


async def execute(app, world, turn):
    run = (await app.state.chat.decide_action(world.owner, UUID(turn["turn_id"]), confirm(turn)))["skill_run"]
    for _ in range(10):
        await app.state.skills.tick()
        run = await app.state.skills.get(world.owner, UUID(run["run_id"]))
        for step in run["steps"]:
            if step["task_id"] and step["status"] != "succeeded":
                task = await world.tasks.get(UUID(step["task_id"]), world.owner_id)
                if task.status != "succeeded":
                    await finish_task(world, task.task_id)
        if run["status"] == "succeeded":
            return run
    raise AssertionError("bounded workflow did not complete")


def test_catalog_requires_enabled_approved_permissions_and_exact_send_consent(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        chat = app.state.chat
        assert not (await chat.skill_catalog(world.owner, world.node_id))["skills"]
        version = await prepare(app, world)
        directory = await chat.skill_catalog(world.owner, world.node_id)
        assert len(directory["skills"]) == 1
        assert directory["skills"][0]["artifact_digest"] == version["artifact_digest"]
        assert str(world.node_id) not in json.dumps(directory["skills"])
        conv = await chat.create(world.owner, ConversationCreate(request_id=uuid4()))
        with pytest.raises(Conflict):
            await suggest(app, world, conversation=conv, skills_digest="0"*64)
        assert not await rows(world, "chat_turns") and not await rows(world)
        await app.state.extensions.disable(world.owner, UUID(version["version_id"]))
        assert not (await chat.skill_catalog(world.owner, world.node_id))["skills"]
        with pytest.raises(AccessDenied):
            await chat.skill_catalog(world.node, world.node_id)
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["unselected", "unconsented"])
def test_model_cannot_invent_or_use_an_unconsented_skill(world, monkeypatch, mode):
    app = app_for(world, monkeypatch)
    async def run():
        version = await prepare(app, world)
        app.state.chat.provider.proposal = {"intent": "run_skill", "arguments": {
            "version_id": str(uuid4()) if mode == "unselected" else version["version_id"], "input": {}}}
        _, turn, _ = await suggest(app, world, **({"allow_skills": False, "skills_digest": None} if mode == "unconsented" else {}))
        assert turn["status"] == "failed" and turn["error_code"] == "POLICY_DENIED"
        assert turn["skill_plan"] is None and not await rows(world, "skill_runs") and not await rows(world, "tasks")
        assert turn["cost_status"] == "calculated"
    asyncio.run(run())


def test_three_step_chat_uses_pinned_tool_and_only_summarizes_verified_stats(world, monkeypatch):
    app = app_for(world, monkeypatch, sandbox=True)
    fake = FakeSandbox()
    app.state.extensions.sandbox.transport = fake
    async def run():
        parent = await prepare(app, world)
        tool = await run_approved(app, world, await generate(app, world, "tool", "统计条目", node_id=world.node_id))
        await app.state.extensions.enable(world.owner, UUID(tool["version_id"]))
        child = await run_approved(app, world, await generate(app, world, "skill", "第三步统计", parent=UUID(parent["version_id"])))
        await app.state.extensions.enable(world.owner, UUID(child["version_id"]))
        _, turn, _ = await suggest(app, world)
        assert len(turn["skill_plan"]["steps"]) == 3 and turn["skill_plan"]["steps"][2]["tool_version_id"] == tool["version_id"]
        fake.calls.clear()
        done = await execute(app, world, turn)
        assert done["result"]["stats"]["total"] == 5 and len(fake.calls) == 1
        value = await app.state.chat.preview_result(world.owner, UUID(turn["turn_id"]), ChatResultPreview(fields=["stats"]))
        assert value["data"]["result"]["stats"]["total"] == 5 and value["data"]["truncated"]
        assert value["data"]["counts_scope"] == "returned_entries_only"
        assert len(await rows(world, "task_attempts")) == 2
    asyncio.run(run())


def test_plan_confirmation_is_atomic_idempotent_and_preserves_directory_approval(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        await prepare(app, world)
        conv, turn, submission = await suggest(app, world)
        assert turn["skill_plan"]["ready"] and len(turn["skill_plan"]["steps"]) == 2
        assert turn["skill_plan"]["steps"][1]["approval_required"]
        assert not await rows(world, "tasks") and not await rows(world, "skill_runs")
        assert (await app.state.chat.submit(world.owner, conv["conversation_id"], submission))["turn_id"] == turn["turn_id"]
        first, second = await asyncio.gather(*(app.state.chat.decide_action(world.owner, UUID(turn["turn_id"]), confirm(turn)) for _ in range(2)))
        assert first["skill_run"]["run_id"] == second["skill_run"]["run_id"]
        assert len(await rows(world, "skill_runs")) == 1
        assert (await rows(world))[0]["skill_run_id"] == first["skill_run"]["run_id"]
        assert (await app.state.chat.history(world.owner, conv["conversation_id"]))["active_turn_id"] == turn["turn_id"]
        with pytest.raises(Conflict, match="BUSY"):
            await suggest(app, world, conversation=conv)
        await app.state.skills.tick()
        linked = await app.state.chat.get(world.owner, turn["turn_id"])
        task_id = linked["skill_run"]["steps"][0]["task_id"]
        await finish_task(world, UUID(task_id))
        await app.state.skills.tick(); await app.state.skills.tick()
        waiting = (await app.state.chat.get(world.owner, turn["turn_id"]))["skill_run"]
        assert waiting["steps"][1]["status"] == "approval"
        assert len(await rows(world, "task_attempts")) == 1
        await app.state.chat.recover()
        await app.state.skills.tick()
        restored = (await app.state.chat.get(world.owner, turn["turn_id"]))["skill_run"]
        assert [(step["task_id"], step["status"], step["result"]) for step in restored["steps"]] == [(step["task_id"], step["status"], step["result"]) for step in waiting["steps"]]
        assert len(app.state.chat.provider.calls) == 1
    asyncio.run(run())


def test_chat_association_failure_rolls_back_run_and_ledger(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        await prepare(app, world)
        _, turn, _ = await suggest(app, world)
        async with world.db.transaction() as conn:
            await conn.execute("CREATE TRIGGER fail_m5 BEFORE UPDATE OF skill_run_id ON chat_turns WHEN NEW.skill_run_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'controlled failure'); END")
        with pytest.raises(sqlite3.DatabaseError):
            await app.state.chat.decide_action(world.owner, UUID(turn["turn_id"]), confirm(turn))
        assert not await rows(world, "skill_runs")
        assert (await rows(world))[0]["skill_run_id"] is None
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["digest", "disable", "policy", "scope", "expired", "archive", "reject", "cancel_all", "pause", "new_turn", "node"])
def test_stale_or_unapproved_plan_never_creates_execution(world, monkeypatch, mode):
    app = app_for(world, monkeypatch)
    async def run():
        version = await prepare(app, world)
        conv, turn, _ = await suggest(app, world)
        decision, principal = confirm(turn), world.owner
        if mode == "digest": decision = decision.model_copy(update={"proposal_digest": "0"*64})
        elif mode == "disable": await app.state.extensions.disable(world.owner, UUID(version["version_id"]))
        elif mode == "policy": await world.security.set_grant(world.node_id, "system.info", enabled=False)
        elif mode == "scope": app.state.harness.link.config.nodes.clear()
        elif mode == "expired":
            async with world.db.transaction() as conn:
                await conn.execute("UPDATE chat_turns SET expires_at=?", (timestamp(utc_now()),))
        elif mode == "archive": await app.state.chat.update(world.owner, conv["conversation_id"], ConversationUpdate(archived=True))
        elif mode == "reject": await app.state.chat.decide_action(world.owner, UUID(turn["turn_id"]), decision.model_copy(update={"decision": "reject"}))
        elif mode in {"cancel_all", "pause"}: await app.state.harness.control(world.owner, mode, uuid4())
        elif mode == "new_turn": await suggest(app, world, conversation=conv)
        elif mode == "node": principal = world.node
        with pytest.raises((AccessDenied, Conflict)):
            await app.state.chat.decide_action(principal, UUID(turn["turn_id"]), decision)
        assert not await rows(world, "skill_runs") and not await rows(world, "tasks")
    asyncio.run(run())


def test_missing_parameters_require_new_preview_and_old_digest_is_invalid(world, monkeypatch):
    from test_m4_completion import input_skill
    from nexus.core.extensions import service as extension_module
    monkeypatch.setattr(extension_module, "fixture_generation", lambda *a, **k: json.dumps(input_skill()))
    app = app_for(world, monkeypatch)
    async def run():
        await prepare(app, world)
        _, turn, _ = await suggest(app, world)
        assert not turn["skill_plan"]["ready"]
        with pytest.raises(ValueError):
            await app.state.chat.decide_action(world.owner, UUID(turn["turn_id"]), confirm(turn))
        for invalid in ({"limit": True}, {"limit": 201}, {"limit": 10, "path": "private"}):
            with pytest.raises(ValueError):
                await app.state.chat.preview_plan(world.owner, UUID(turn["turn_id"]), ChatPlanPreview(input=invalid))
        value = await app.state.chat.preview_plan(world.owner, UUID(turn["turn_id"]), ChatPlanPreview(input={"limit": 10}))
        assert value["plan"]["ready"] and value["plan"]["steps"][1]["arguments"]["limit"] == 10
        with pytest.raises(Conflict):
            await app.state.chat.decide_action(world.owner, UUID(turn["turn_id"]), confirm(turn))
        current = await app.state.chat.get(world.owner, turn["turn_id"])
        run = (await app.state.chat.decide_action(world.owner, UUID(turn["turn_id"]), confirm(current)))["skill_run"]
        assert run["input"] == {"limit": 10}
    asyncio.run(run())


def test_successful_steps_get_preview_and_one_final_call_in_original_budget(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        await prepare(app, world)
        conv, turn, _ = await suggest(app, world)
        done = await execute(app, world, turn)
        chat = app.state.chat
        assert not await chat.process_one() and len(chat.provider.calls) == 1
        preview = await chat.preview_result(world.owner, UUID(turn["turn_id"]), ChatResultPreview(fields=["directory"]))
        assert preview["data"]["verification"] == "skill_steps_verified" and preview["data"]["truncated"]
        assert set(preview["data"]["result"]) == {"directory"} and preview["data"]["omitted_fields"] == ["system"]
        consent = ChatResultConsent(result_digest=preview["result_digest"], expected_revision=preview["expected_revision"], allow_model=True)
        await chat.consent_result(world.owner, UUID(turn["turn_id"]), consent)
        await chat.process_one()
        await chat.consent_result(world.owner, UUID(turn["turn_id"]), consent)
        assert not await chat.process_one()
        final = await chat.get(world.owner, turn["turn_id"])
        assert final["phase"] == "summary" and final["model_call_count"] == 2
        calls = await rows(world)
        assert all(c["skill_run_id"] == done["run_id"] for c in calls)
        assert calls[1]["reserved_microunits"] + calls[0]["cost_microunits"] == 50000
        messages, options = chat.provider.calls[1]
        assert options["final_result"] and not options["allow_tools"] and "skill_catalog" not in options
        assert messages[-1]["tool_call_id"] == "actual-skill-call"
        assert str(world.node_id) not in json.dumps(messages)
        assert json.loads(messages[-1]["content"]) == preview["data"]
        assert len(await rows(world, "task_attempts")) == 2
        assert (await chat.history(world.owner, conv["conversation_id"]))["active_turn_id"] is None
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["unknown", "failure", "stop", "policy", "changed_result", "unknown_cost", "budget"])
def test_invalid_results_and_limits_block_final_exchange(world, monkeypatch, mode):
    app = app_for(world, monkeypatch)
    async def run():
        await prepare(app, world)
        _, turn, _ = await suggest(app, world)
        chat = app.state.chat
        if mode == "stop":
            root = (await chat.decide_action(world.owner, UUID(turn["turn_id"]), confirm(turn)))["skill_run"]
            await app.state.skills.cancel(world.owner, UUID(root["run_id"])); await app.state.skills.tick()
        else:
            await execute(app, world, turn)
            if mode not in {"unknown_cost", "budget"}:
                async with world.db.transaction() as conn:
                    if mode == "unknown": await conn.execute("UPDATE tasks SET execution_outcome='unknown'")
                    elif mode == "failure": await conn.execute("UPDATE skill_runs SET status='failed'")
                    elif mode == "changed_result": await conn.execute("UPDATE skill_run_steps SET result_json='{}' WHERE step_index=1")
                if mode == "policy": await world.security.set_grant(world.node_id, "system.info", enabled=False)
        if mode in {"unknown_cost", "budget"}:
            value = await chat.preview_result(world.owner, UUID(turn["turn_id"]), ChatResultPreview())
            await chat.consent_result(world.owner, UUID(turn["turn_id"]), ChatResultConsent(result_digest=value["result_digest"], expected_revision=value["expected_revision"], allow_model=True))
            async with world.db.transaction() as conn:
                await conn.execute("UPDATE model_calls SET cost_status='unknown'" if mode == "unknown_cost" else "UPDATE model_calls SET cost_microunits=49999")
            assert not await chat.process_one()
        else:
            with pytest.raises((AccessDenied, ValueError)):
                await chat.preview_result(world.owner, UUID(turn["turn_id"]), ChatResultPreview())
        assert len(chat.provider.calls) == len(await rows(world)) == 1
    asyncio.run(run())
