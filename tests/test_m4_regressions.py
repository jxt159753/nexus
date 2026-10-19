"""Regressions from independent M4 review; no production or generated code runs."""
import asyncio
import json
import threading
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from test_extensions_m4 import app_for, grants, generate, run_approved, finish_task, FakeSandbox
from test_node_link import core_config
from nexus.core.app import create_app
from nexus.core.config import ExtensionConfig, SandboxNodePolicy, TrustedNode
from nexus.core.models.config import ModelConfig
from nexus.core.models.providers import ModelReply
from nexus.core.extensions.fixtures import fixture_generation
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one
from nexus.contracts.extensions import ExtensionRequestCreate, ExtensionGenerateConsent, canonical, digest_value
from nexus.contracts.sandbox import SandboxHello, SandboxExecute, SandboxWelcome, SandboxCancel, encode_sandbox_message, parse_sandbox_message
from nexus.sandbox.node import SandboxNode, Receipts


async def approved_skill(app, world):
    await grants(world)
    await grants(world, 'filesystem.list')
    version = await run_approved(app, world, await generate(app, world, 'skill', '工作区概览'))
    await app.state.extensions.enable(world.owner, UUID(version['version_id']))
    return version


def test_disable_must_prevent_dispatch_of_unstarted_step(world):
    async def run():
        app = app_for(world)
        version = await approved_skill(app, world)
        run = await app.state.skills.start(world.owner, UUID(version['version_id']), world.node_id, {})
        await app.state.skills.tick()
        run = await app.state.skills.get(world.owner, UUID(run['run_id']))
        task_id = UUID(run['steps'][0]['task_id'])
        await app.state.extensions.disable(world.owner, UUID(version['version_id']))
        await app.state.skills.tick()
        stopped = await app.state.skills.get(world.owner, UUID(run['run_id']))
        command = await app.state.harness.executions.dispatch(task_id, world.node, world.scope, uuid4(), uuid4(), 10)
        assert command is None, f"Disabled Skill is {stopped['status']} but unstarted task still dispatched: {command.tool}"
    asyncio.run(run())


def test_three_step_skill_must_route_tool_to_distinct_sandbox_node(world):
    async def run():
        sandbox_id = uuid4()
        await world.security.register_identity(sandbox_id, 'node', SecretStr('s' * 43))
        app = create_app(core_config(world,
            extensions=ExtensionConfig(enabled=True, fixture_generation=True),
            nodes={world.node_id: TrustedNode(tools=frozenset({'system.info@1', 'filesystem.list@1'}), roots=frozenset({'workspace'}))},
            sandbox_nodes={sandbox_id: SandboxNodePolicy(image_digest='a' * 64)}))
        app.state.extensions.sandbox.transport = FakeSandbox()
        parent = await approved_skill(app, world)
        tool = await run_approved(app, world, await generate(app, world, 'tool', '统计条目', node_id=sandbox_id))
        await app.state.extensions.enable(world.owner, UUID(tool['version_id']))
        skill = await run_approved(app, world, await generate(app, world, 'skill', '第三步统计条目', parent=UUID(parent['version_id'])))
        await app.state.extensions.enable(world.owner, UUID(skill['version_id']))
        run = await app.state.skills.start(world.owner, UUID(skill['version_id']), world.node_id, {})
        await app.state.skills.tick()
        run = await app.state.skills.get(world.owner, UUID(run['run_id']))
        await finish_task(world, UUID(run['steps'][0]['task_id']))
        await app.state.skills.tick(); await app.state.skills.tick()
        run = await app.state.skills.get(world.owner, UUID(run['run_id']))
        await finish_task(world, UUID(run['steps'][1]['task_id']))
        for _ in range(4):
            await app.state.skills.tick()
        done = await app.state.skills.get(world.owner, UUID(run['run_id']))
        assert done['status'] == 'succeeded', f"Distinct Windows/Sandbox nodes produce {done['status']}: {done['error']}"
    asyncio.run(run())


def test_cancel_all_must_not_kill_generation_worker(world, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-only-never-used')
    async def run():
        app = create_app(core_config(world, models=ModelConfig(enabled=True),
            extensions=ExtensionConfig(enabled=True, generation_enabled=True)))
        began = asyncio.Event()
        class Provider:
            calls = 0
            async def generate(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    began.set()
                    await asyncio.Event().wait()
                return ModelReply(fixture_generation('skill', '工作区概览', parent_version_id=None, tool_version_id=None), 100, 100, True, model='deepseek-test')
        service = app.state.extensions
        service.provider = Provider()
        item = ExtensionRequestCreate(request_id=uuid4(), kind='skill', need='测试取消')
        created = await service.create(world.owner, item)
        await service.generate(world.owner, item.request_id, ExtensionGenerateConsent(preview_digest=created['preview_digest'], allow_model=True))
        worker = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(began.wait(), 2)
            await app.state.harness.control(world.owner, 'cancel_all', uuid4())
            await asyncio.wait({worker}, timeout=.8)
            assert not worker.done(), 'cancel_all cancelled the long-lived generation worker; new requests will remain queued'
            second = ExtensionRequestCreate(request_id=uuid4(), kind='skill', need='工作区概览')
            created = await service.create(world.owner, second)
            await service.generate(world.owner, second.request_id, ExtensionGenerateConsent(preview_digest=created['preview_digest'], allow_model=True))
            async with asyncio.timeout(3):
                while (await service.get_request(world.owner, second.request_id))['status'] != 'candidate':
                    await asyncio.sleep(.05)
            assert service.provider.calls == 2
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())


def test_cancel_all_must_block_a_step_paused_before_submit(world):
    async def run():
        app = app_for(world)
        version = await approved_skill(app, world)
        run = await app.state.skills.start(world.owner, UUID(version['version_id']), world.node_id, {})
        ready, release = asyncio.Event(), asyncio.Event()
        original = app.state.skills._step_task
        async def delayed(*args):
            ready.set()
            await release.wait()
            return await original(*args)
        app.state.skills._step_task = delayed
        tick = asyncio.create_task(app.state.skills.tick())
        try:
            await asyncio.wait_for(ready.wait(), 2)
            await app.state.harness.control(world.owner, 'cancel_all', uuid4())
            release.set()
            await tick
            run = await app.state.skills.get(world.owner, UUID(run['run_id']))
            task_id = run['steps'][0]['task_id']
            command = None if task_id is None else await app.state.harness.executions.dispatch(UUID(task_id), world.node, world.scope, uuid4(), uuid4(), 10)
            assert command is None, 'A new device command was created and dispatched after cancel_all committed'
        finally:
            release.set()
            await asyncio.gather(tick, return_exceptions=True)
    asyncio.run(run())


def test_approval_policy_binding_must_be_rechecked(world):
    async def run():
        app = app_for(world)
        version = await approved_skill(app, world)
        async with world.db.transaction(write=False) as conn:
            old = (await one(conn, 'SELECT version FROM policy_state WHERE singleton=1'))[0]
        await world.security.set_grant(world.node_id, 'system.info', enabled=False)
        async with world.db.transaction(write=False) as conn:
            new = (await one(conn, 'SELECT version FROM policy_state WHERE singleton=1'))[0]
        assert new > old
        with pytest.raises(AccessDenied, match='.*'):
            await app.state.skills.start(world.owner, UUID(version['version_id']), world.node_id, {})
    asyncio.run(run())


def test_sandbox_node_must_process_cancel_while_tool_is_running(tmp_path):
    async def run():
        entered, release = threading.Event(), threading.Event()
        signals = []
        class Runtime:
            def execute(self, *args, **kwargs):
                signals.append(kwargs['cancel_event'])
                entered.set()
                release.wait(3)
                return {'outcome': 'failed', 'error_code': 'SANDBOX_CANCELLED', 'stopped': True}
        node = object.__new__(SandboxNode)
        from types import SimpleNamespace
        node_id = uuid4()
        node.config = SimpleNamespace(node_id=node_id, image_digest='a' * 64,
            platform='systrap', runsc_digest='b' * 64, max_result_bytes=32768, execution_timeout_seconds=10)
        node.runtime, node._available, node._active = Runtime(), True, None
        node.receipts = Receipts(tmp_path / 'receipts.sqlite3')
        node.hello = lambda: SandboxHello(
            node_id=uuid4(), node_version='test', boot_id=uuid4(), runtime='runsc', runtime_version='runsc test', image_digest='a' * 64,
            available=True, reason=None, verification_profiles=['directory_entry_stats_v1'])
        session, execution = uuid4(), uuid4()
        document = {'kind': 'tool', 'source': 'inert text; never executed', 'environment': {
            'image_digest': 'a' * 64, 'platform': 'systrap', 'runsc_digest': 'b' * 64,
            'policy': 'runsc-host-limits-v2', 'node_id': str(node_id)}}
        command = SandboxExecute(session_id=session, execution_id=execution, version_id=uuid4(),
            artifact_digest=digest_value(document), environment_digest=digest_value(document['environment']),
            release_json=canonical(document), input_json='{}', timeout_seconds=10)
        ack = asyncio.Event()
        class WebSocket:
            def __init__(self): self.queue = asyncio.Queue()
            async def recv(self): return await self.queue.get()
            async def send(self, text):
                if parse_sandbox_message(text).type == 'sandbox.cancelled': ack.set()
        ws = WebSocket()
        await ws.queue.put(encode_sandbox_message('sandbox.welcome', SandboxWelcome(session_id=session,
            heartbeat_interval_seconds=1, heartbeat_timeout_seconds=30, max_message_bytes=65536,
            max_result_bytes=32768, execution_timeout_seconds=10)))
        await ws.queue.put(encode_sandbox_message('sandbox.execute', command))
        connection = asyncio.create_task(node._connected(ws))
        before_completion = False
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await ws.queue.put(encode_sandbox_message('sandbox.cancel', SandboxCancel(session_id=session, execution_id=execution)))
            try:
                await asyncio.wait_for(ack.wait(), .5)
                before_completion = True
            except TimeoutError:
                pass
            assert signals[0].is_set()
            connection.cancel()
            await asyncio.sleep(.05)
            assert not connection.done(), 'Disconnect released the slot before execution cleanup'
            assert node._active == execution
        finally:
            release.set()
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
            node.receipts.close()
        assert before_completion, 'Sandbox cancel was only read after the blocking execution returned'
    asyncio.run(run())


def test_generation_token_ceiling_must_fit_reserved_root_budget(world):
    models = ModelConfig(enabled=True, chat_enabled=True, input_price=300_000, output_price=15_000_000)
    extensions = ExtensionConfig(enabled=True, generation_enabled=True)
    worst = (16384 * models.input_price + extensions.max_output_tokens * models.output_price + 999999) // 1000000
    assert worst > models.task_cost_microunits, 'Fixture must exceed the generation budget while preserving original model checks'
    with pytest.raises(ValueError):
        core_config(world, models=models, extensions=extensions)


def test_preview_exposes_fixed_tool_ids_and_parent_plan_without_source(world):
    async def run():
        app = app_for(world, sandbox=True)
        parent = await approved_skill(app, world)
        app.state.extensions.sandbox.transport = FakeSandbox()
        tool = await run_approved(app, world, await generate(app, world, 'tool', '统计', node_id=world.node_id))
        await app.state.extensions.enable(world.owner, UUID(tool['version_id']))
        # Only build a draft preview; no model consent or call occurs.
        app.state.extensions.config = ExtensionConfig(enabled=True, generation_enabled=True)
        value = ExtensionRequestCreate(request_id=uuid4(), kind='skill', need='追加第三步', parent_version_id=UUID(parent['version_id']))
        preview = await app.state.extensions.create(world.owner, value)
        outbound = canonical(preview['outbound'])
        assert tool['version_id'] in outbound
        assert 'filesystem.list' in outbound and parent['version_id'] in outbound
        assert tool['source'] not in outbound
        assert 'source' not in preview['outbound']['messages'][1]['content']
        await app.state.extensions.disable(world.owner, UUID(tool['version_id']))
        other = await app.state.extensions.create(world.owner, value.model_copy(update={'request_id': uuid4()}))
        assert tool['version_id'] not in canonical(other['outbound'])
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, 'SELECT count(*) FROM model_calls'))[0] == 0
    asyncio.run(run())
