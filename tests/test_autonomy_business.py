"""Independent suite gates and rollback; sandbox transport is simulated here."""

import asyncio
import json
import os
from uuid import UUID, uuid4

import pytest

from nexus.contracts.task import PluginNodeRequest
from nexus.core.config import TrustedNode
from nexus.core.plugins import business
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one
from nexus.core.storage.execution import ExecutionRepository
from nexus.node.config import NodeConfig
from nexus.node.runtime import NodeRuntime
from nexus.plugins.config import PluginNodeConfig
from nexus.plugins.contracts import PluginNodeArguments, PluginRelease
from test_chat_skills import app_for
from test_chat_work import setup
from test_plugin_lifecycle import candidate, full


async def prepare(app, world):
    chat = app.state.chat; chat.plugins.config.enabled = True
    await chat.plugins.seed(); await full(chat, world.owner)
    conv, _, _ = await setup(app, world, []); await chat.process_one()
    chat.plugins.tester.transport.status = lambda _: {'available': True}
    async def execute(*args, **kwargs):
        value = args[2]['input']
        return {'outcome': 'succeeded', 'output': {'sum': value['a'] + value['b']}}
    chat.plugins.tester.transport.execute = execute
    return conv


async def _tested(app, world, conv, release):
    service = app.state.chat.plugins
    version = await service.install(world.owner, conv['conversation_id'], uuid4(), release)
    started = await service.tester.start(world.owner, conv['conversation_id'], version['version_id'])
    async with world.db.transaction(write=False) as conn:
        run = await one(conn, 'SELECT * FROM plugin_test_runs WHERE test_run_id=?', (started['test_run_id'],))
    await service.tester.process(run, asyncio.Event())
    async with world.db.transaction(write=False) as conn:
        return await one(conn, 'SELECT * FROM plugin_versions WHERE version_id=?', (version['version_id'],))


def suite(**changes):
    return business.BusinessSuite(expected_revision=0, cases=[{'tool': 'add', 'input': {'a': 3, 'b': 4}, 'expected': {'sum': 7}}], **changes)


def test_independent_suite_failure_blocks_enable_and_freezes_revision(world, monkeypatch):
    app = app_for(world, monkeypatch, sandbox=True)
    async def run():
        conv = await prepare(app, world)
        async with world.db.transaction() as conn:
            value = suite(); value.cases[0].expected = {'sum': 8}
            await business.configure(conn, world.owner_id, 'user.add_numbers', value)
        row = await _tested(app, world, conv, candidate())
        evidence = json.loads(row['test_json'])
        assert row['test_status'] == 'failed' and not row['enabled']
        assert evidence['cases'][0]['matched'] and not evidence['cases'][1]['matched']
        assert evidence['business_verified'] is False and evidence['host_safety_verified'] is False
        async with world.db.transaction() as conn:
            with pytest.raises(AccessDenied, match='PLUGIN_BUSINESS_TEST_REQUIRED'):
                await app.state.chat.plugins.decide(conn, world.owner, row, conv['conversation_id'], 'enable')
    asyncio.run(run())


def test_business_suite_revision_invalidates_previous_success(world, monkeypatch):
    app = app_for(world, monkeypatch, sandbox=True)
    async def run():
        conv = await prepare(app, world)
        async with world.db.transaction() as conn: await business.configure(conn, world.owner_id, 'user.add_numbers', suite())
        row = await _tested(app, world, conv, candidate())
        assert row['enabled'] and json.loads(row['test_json'])['business_verified']
        async with world.db.transaction() as conn:
            changed = suite(); changed.expected_revision = 1
            await business.configure(conn, world.owner_id, 'user.add_numbers', changed)
            with pytest.raises(AccessDenied, match='PLUGIN_BUSINESS_TEST_REQUIRED'): await business.require_business(conn, row)
    asyncio.run(run())


@pytest.mark.skipif(os.name != 'nt', reason='real Windows plugin process')
@pytest.mark.parametrize('revoked', [False, True])
def test_runtime_failure_rolls_back_only_to_approved_matching_baseline(world, monkeypatch, tmp_path, revoked):
    app = app_for(world, monkeypatch, sandbox=True)
    async def run():
        conv = await prepare(app, world); service = app.state.chat.plugins
        policy = PluginNodeConfig(enabled=True, allow_host_python=True)
        app.state.harness.link.config.nodes[world.node_id] = TrustedNode(tools=frozenset({'plugin.call@1'}), roots=frozenset({'workspace'}), plugin_digest=policy.digest())
        for identity in (world.owner_id, world.node_id): await world.security.set_grant(identity, 'plugin.call', root_id='workspace')
        baseline = await _tested(app, world, conv, candidate())
        async with world.db.transaction() as conn: await business.configure(conn, world.owner_id, 'user.add_numbers', suite(rollback_on_failure=True))
        release = candidate().model_dump(mode='json'); release['manifest']['version'] = '1.0.1'
        release['source'] = "def handle(tool, input, context):\n    return {'sum':input['a']+input['b'] if context.testing else 999}\n"
        candidate_release = PluginRelease.model_validate(release)
        installed = await _tested(app, world, conv, candidate_release)
        assert installed['enabled'] and json.loads(installed['test_json'])['comparison']['baseline_passed'] == 1
        request = PluginNodeRequest(request_id=uuid4(), intent='execute_plugin', arguments=PluginNodeArguments(
            plugin_id='user.add_numbers', tool='add', input={'a': 3, 'b': 4}, release_digest=installed['digest'],
            context_id=UUID(conv['conversation_id']), invocation_id=uuid4(), release=candidate_release))
        task = await app.state.harness.submit(world.owner, world.node_id, request)
        if task.status == 'awaiting_approval':
            async with world.db.transaction(write=False) as conn:
                approval = await one(conn, 'SELECT approval_id FROM approvals WHERE task_id=? AND decision IS NULL', (str(task.task_id),))
            await world.security.decide(UUID(approval[0]), world.owner, 'approve', app.state.harness.scope(world.node_id))
        repo = ExecutionRepository(world.db, world.security, 32768)
        command = await repo.dispatch(task.task_id, world.node, app.state.harness.scope(world.node_id), uuid4(), uuid4(), 10)
        node_dir = tmp_path / 'node'; node_dir.mkdir()
        with NodeRuntime(NodeConfig(node_id=world.node_id, data_dir=node_dir, roots={'workspace': tmp_path}, allowed_tools=frozenset({'plugin.call@1'}), plugins=policy)) as runtime:
            answer = await asyncio.to_thread(runtime.execute, command)
        assert answer.phase == 'completed' and answer.execution_stopped and answer.result.outcome == 'succeeded'
        assert answer.result.result.data == {'sum': 999}
        await repo.store_result(world.node, answer.result); await repo.verify(command.attempt_id)
        if revoked:
            async with world.db.transaction() as conn:
                fallback = await one(conn, 'SELECT * FROM plugin_versions WHERE version_id=?', (baseline['version_id'],))
                await service.decide(conn, world.owner, fallback, conv['conversation_id'], 'disable')
        await business.observe(service); await business.observe(service)
        async with world.db.transaction(write=False) as conn:
            result = await one(conn, 'SELECT * FROM plugin_rollouts WHERE version_id=?', (installed['version_id'],))
            assert result['status'] == ('blocked' if revoked else 'rolled_back')
            assert result['observations'] == 1 and result['failures'] == 1
            assert not (await one(conn, 'SELECT enabled FROM plugin_versions WHERE version_id=?', (installed['version_id'],)))[0]
            assert bool((await one(conn, 'SELECT enabled FROM plugin_versions WHERE version_id=?', (baseline['version_id'],)))[0]) is (not revoked)
            assert (await one(conn, 'SELECT count(*) FROM tasks'))[0] == 1
    asyncio.run(run())
