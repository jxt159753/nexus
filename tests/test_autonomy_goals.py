"""Goal assertions use real file-task evidence, never a model success string."""

import asyncio
import hashlib
import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from nexus.contracts.autonomy import GoalPlan, GoalCheckpoint, GoalPolicy
from nexus.core.plugins import goals, lifecycle, core_tools
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one
from nexus.core.storage.tasks import Conflict
from test_chat_skills import app_for
from test_chat_work import basic, file_task, setup
from test_plugin_lifecycle import full


def plan(**changes):
    value = {'expected_revision': 0, 'constraints': ['只能操作本任务工作区'],
             'criteria': [{'id': 'artifact', 'description': '真实文件读回与预期完全一致', 'category': 'artifact',
                           'tool': 'filesystem.read', 'arguments': [{'path': '/path', 'equals': 'evidence.txt'}, {'path': '/root_id', 'equals': 'workspace'}],
                           'assertions': [{'path': '/text', 'equals': 'verified effect'}, {'path': '/sha256', 'equals': hashlib.sha256(b'verified effect').hexdigest()}]}],
             'steps': [{'id': 'read', 'instruction': '读取文件并核对内容', 'criteria': ['artifact']}]}
    return GoalPlan.model_validate(value | changes)


async def ready(app, world, tmp_path):
    chat = app.state.chat; chat.plugins.config.enabled = True
    await chat.plugins.seed(); await full(chat, world.owner); await basic(app, world)
    (tmp_path / 'evidence.txt').write_text('verified effect', encoding='utf-8')
    conv, grant, turn = await setup(app, world, [
        {'intent': 'plugin_execute', 'arguments': {'plugin_id': 'builtin.goal', 'tool': 'create_goal', 'input': {'objective': '验证文件产物', 'max_rounds': 8}}},
        {'intent': 'read_file', 'arguments': {'root_id': 'workspace', 'path': 'evidence.txt'}}])
    for _ in range(4): await chat.process_one()
    current = await chat.get(world.owner, turn['turn_id'])
    task_id = current['work_steps'][-1]['task_id']
    assert task_id, current
    await file_task(app, world, task_id, tmp_path)
    for _ in range(3): await chat.process_one()
    async with world.db.transaction(write=False) as conn:
        goal = await one(conn, 'SELECT * FROM plugin_goals')
    return conv, grant, turn, goal, task_id


def test_verified_completion_and_saved_context_survive_reopen(world, monkeypatch, tmp_path):
    app = app_for(world, monkeypatch)
    async def run():
        conv, grant, turn, goal, task_id = await ready(app, world, tmp_path)
        async with world.db.transaction() as conn:
            with pytest.raises(AccessDenied, match='GOAL_ACCEPTANCE_REQUIRED'):
                await goals.verify_completion(conn, goal)
            await goals.set_plan(conn, goal, plan())
            with pytest.raises(AccessDenied, match='GOAL_STEPS_INCOMPLETE'):
                await goals.verify_completion(conn, goal)
            result = await goals.checkpoint(conn, goal, GoalCheckpoint(expected_revision=1, step_id='read', status='succeeded',
                evidence=[{'criterion_id': 'artifact', 'task_id': UUID(task_id)}], note='文件读回已核对', remaining=[]), turn_id=turn['turn_id'])
            assert result['plan_revision'] == 2 and result['evidence'][0]['task_id'] == task_id
            done = await core_tools.execute(app.state.chat.plugins, conn, world.owner, conv, {}, grant, 'builtin.goal', 'update_goal', {'goal_id': goal['goal_id'], 'status': 'complete'})
            assert done['completion_verified'] is True
        await world.db.initialize()
        async with world.db.transaction(write=False) as conn:
            saved = await goals.snapshot(conn, goal['goal_id'])
            assert saved['completion_verified'] and saved['verification'] == 'declared_task_assertions'
            assert saved['plan']['constraints'] == ['只能操作本任务工作区']
            assert saved['checkpoints'][0]['note'] == '文件读回已核对'
    asyncio.run(run())


@pytest.mark.parametrize('change', ['wrong_path', 'wrong_result', 'unknown', 'unstopped', 'other_goal', 'changed_result'])
def test_evidence_rejects_mismatch_unknown_and_unrelated_tasks(world, monkeypatch, tmp_path, change):
    app = app_for(world, monkeypatch)
    async def run():
        _, _, _, goal, task_id = await ready(app, world, tmp_path)
        async with world.db.transaction() as conn:
            value = plan()
            if change == 'wrong_path': value.criteria[0].arguments[0].equals = 'unrelated.txt'
            if change == 'wrong_result': value.criteria[0].assertions[0].equals = 'fabricated success'
            await goals.set_plan(conn, goal, value)
            if change == 'unknown': await conn.execute("UPDATE tasks SET execution_outcome='unknown' WHERE task_id=?", (task_id,))
            if change == 'unstopped': await conn.execute('UPDATE task_attempts SET execution_stopped=0 WHERE task_id=?', (task_id,))
            if change == 'other_goal': await conn.execute('DELETE FROM plugin_authorities WHERE authority_id=?', (goal['goal_id'],))
            check = GoalCheckpoint(expected_revision=1, step_id='read', status='succeeded', evidence=[{'criterion_id': 'artifact', 'task_id': UUID(task_id)}])
            if change == 'changed_result':
                await goals.checkpoint(conn, goal, check)
                row = await one(conn, 'SELECT result_json FROM tasks WHERE task_id=?', (task_id,))
                altered = json.loads(row[0]); altered['text'] = 'changed'
                await conn.execute('UPDATE tasks SET result_json=? WHERE task_id=?', (json.dumps(altered), task_id))
                with pytest.raises(AccessDenied): await goals.verify_completion(conn, goal)
            else:
                with pytest.raises((AccessDenied, Conflict)): await goals.checkpoint(conn, goal, check)
    asyncio.run(run())


def test_plan_dependencies_replanning_limits_and_no_progress(world, monkeypatch, tmp_path):
    app = app_for(world, monkeypatch)
    async def run():
        _, _, _, goal, task_id = await ready(app, world, tmp_path)
        value = plan()
        value.criteria.append(value.criteria[0].model_copy(update={'id': 'check'}))
        value.steps.append(value.steps[0].model_copy(update={'id': 'confirm', 'depends_on': ['read'], 'criteria': ['check']}))
        async with world.db.transaction() as conn:
            await goals.set_plan(conn, goal, value)
            with pytest.raises(AccessDenied, match='GOAL_DEPENDENCY_PENDING'):
                await goals.checkpoint(conn, goal, GoalCheckpoint(expected_revision=1, step_id='confirm', status='succeeded'))
            await goals.checkpoint(conn, goal, GoalCheckpoint(expected_revision=1, step_id='read', status='succeeded', evidence=[{'criterion_id': 'artifact', 'task_id': UUID(task_id)}]))
            with pytest.raises(AccessDenied,match='GOAL_EVIDENCE_PRECEDES_DEPENDENCY'):
                await goals.checkpoint(conn,goal,GoalCheckpoint(expected_revision=2,step_id='confirm',status='succeeded',evidence=[{'criterion_id':'check','task_id':UUID(task_id)}]))
            weakened = value.model_copy(deep=True); weakened.expected_revision = 2; weakened.reason = 'change'; weakened.criteria[0].assertions[0].equals = 'other'
            with pytest.raises(AccessDenied, match='GOAL_ACCEPTANCE_IMMUTABLE'): await goals.set_plan(conn, goal, weakened)
            for revision in range(2, 5):
                value.expected_revision = revision; value.reason = '保留完成证据，调整剩余步骤'
                await goals.set_plan(conn, goal, value)
            value.expected_revision = 5
            with pytest.raises(AccessDenied, match='GOAL_REPLAN_LIMIT'): await goals.set_plan(conn, goal, value)
            assert (await goals.next_step(conn, goal['goal_id']))['step_id'] == 'confirm'
            await conn.execute('UPDATE plugin_goals SET rounds=rounds+3 WHERE goal_id=?', (goal['goal_id'],))
            fresh = await one(conn, 'SELECT * FROM plugin_goals WHERE goal_id=?', (goal['goal_id'],))
            with pytest.raises(AccessDenied, match='GOAL_NO_VERIFIED_PROGRESS'): await goals.continuation(conn, fresh)
    asyncio.run(run())


def test_owner_opted_restart_continues_one_segment_and_preserves_root(world, monkeypatch, tmp_path):
    app = app_for(world, monkeypatch)
    async def run():
        _, _, turn, goal, _ = await ready(app, world, tmp_path)
        async with world.db.transaction() as conn:
            await goals.set_plan(conn, goal, plan())
            await goals.policy(conn, goal, GoalPolicy(expected_revision=0, resume_after_restart=True, priority=3))
        await lifecycle.recover(app.state.chat.plugins)
        await lifecycle.tick(app.state.chat.plugins); await lifecycle.tick(app.state.chat.plugins)
        async with world.db.transaction(write=False) as conn:
            authority = await one(conn, 'SELECT * FROM plugin_authorities')
            assert authority['root_turn_id'] == turn['turn_id']
            assert authority['state'] == 'active'
            assert (await one(conn, 'SELECT count(*) FROM plugin_wakeups'))[0] == 1
            assert (await goals.snapshot(conn, goal['goal_id']))['steps'][0]['status'] == 'running'
    asyncio.run(run())


def test_crash_after_submit_reconciles_round_and_step_once(world, monkeypatch, tmp_path):
    app = app_for(world, monkeypatch)
    async def run():
        _, _, root, goal, _ = await ready(app, world, tmp_path)
        async with world.db.transaction() as conn: await goals.set_plan(conn, goal, plan())
        await lifecycle.tick(app.state.chat.plugins)
        async with world.db.transaction() as conn:
            wake = await one(conn, 'SELECT * FROM plugin_wakeups')
            assert wake['goal_step_id'] == 'read'
            await conn.execute("UPDATE plugin_wakeups SET status='prepared',turn_id=NULL")
            await conn.execute('UPDATE plugin_authorities SET last_turn_id=?', (root['turn_id'],))
            await conn.execute('UPDATE plugin_goals SET rounds=1')
            await conn.execute("UPDATE goal_steps SET status='pending',turn_id=NULL")
        await lifecycle.recover(app.state.chat.plugins); await lifecycle.recover(app.state.chat.plugins)
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, 'SELECT rounds FROM plugin_goals'))[0] == 2
            assert (await one(conn, 'SELECT turn_id FROM plugin_wakeups'))[0] == wake['turn_id']
            assert (await one(conn, 'SELECT turn_id FROM goal_steps'))[0] == wake['turn_id']
    asyncio.run(run())


def test_manual_reattach_keeps_original_cost_root_and_counts_new_turn(world, monkeypatch, tmp_path):
    from uuid import uuid4
    from nexus.contracts.chat import ChatSubmission
    from nexus.core.cognitive.work_catalog import catalog
    from test_chat_work import WorkProvider
    app = app_for(world, monkeypatch)
    async def run():
        conv, grant, root, goal, _ = await ready(app, world, tmp_path)
        chat = app.state.chat
        chat.provider = WorkProvider([{'intent':'plugin_execute','arguments':{'plugin_id':'builtin.goal','tool':'update_goal','input':{'goal_id':goal['goal_id'],'status':'active'}}}])
        async with world.db.transaction(write=False) as conn:
            current = await chat.conversation(conn, world.owner, conv['conversation_id'])
            directory = await catalog(chat, conn, world.owner, world.node_id)
        turn = await chat.submit(world.owner, conv['conversation_id'], ChatSubmission(request_id=uuid4(), expected_revision=current['revision'],
            text='继续原目标并保留已完成工作', allow_model=True, allow_tools=True, node_id=world.node_id,
            work_grant_id=UUID(grant['grant_id']), work_digest=directory['work_digest'], allow_work_results=True))
        for _ in range(4): await chat.process_one()
        async with world.db.transaction(write=False) as conn:
            authority = await one(conn, 'SELECT * FROM plugin_authorities')
            assert authority['root_turn_id'] == root['turn_id'] and authority['last_turn_id'] == turn['turn_id']
            assert (await one(conn, 'SELECT rounds FROM plugin_goals'))[0] == 2
            assert (await one(conn, 'SELECT turn_id FROM plugin_wakeups'))[0] == turn['turn_id']
            assert await lifecycle.budget(conn, chat, turn['turn_id'], 50000) < 50000
    asyncio.run(run())


def test_older_step_context_is_available_without_filling_every_model_response(world, monkeypatch, tmp_path):
    app = app_for(world, monkeypatch)
    async def run():
        _, _, _, goal, _ = await ready(app, world, tmp_path)
        value = plan()
        value.steps.append(value.steps[0].model_copy(update={'id':'inspect','instruction':'核对其余条件'}))
        async with world.db.transaction() as conn:
            await goals.set_plan(conn, goal, value)
            await goals.checkpoint(conn, goal, GoalCheckpoint(expected_revision=1, step_id='read', status='failed', note='原失败原因', remaining=['仍须完成文件读回']))
            for revision in range(2, 5):
                await goals.checkpoint(conn, goal, GoalCheckpoint(expected_revision=revision, step_id='inspect', status='blocked', note='等待输入'))
            summary = goals.model_view(await goals.snapshot(conn, goal['goal_id']))
            assert all(item['step_id']=='inspect' for item in summary['checkpoints'])
            detail = goals.model_view(await goals.snapshot(conn, goal['goal_id'], step_id='read'))
            assert detail['step_checkpoint']['remaining'] == ['仍须完成文件读回']
            assert detail['plan']['expected_revision'] == detail['plan_revision'] == 5
    asyncio.run(run())


def test_model_cannot_replace_a_paused_goal_to_reset_root_limits(world, monkeypatch, tmp_path):
    app = app_for(world, monkeypatch)
    async def run():
        conv, grant, root, goal, _ = await ready(app, world, tmp_path)
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE plugin_goals SET status='paused'")
            with pytest.raises(AccessDenied,match='GOAL_NEW_USER_TURN_REQUIRED'):
                await core_tools.execute(app.state.chat.plugins,conn,world.owner,conv,{'turn_id':root['turn_id']},grant,
                    'builtin.goal','create_goal',{'objective':'换个目标名继续原工作','max_rounds':20})
            assert (await one(conn,'SELECT count(*) FROM plugin_goals'))[0] == 1
    asyncio.run(run())


@pytest.mark.parametrize('private', ['revoked_memory', 'explicit_reference'])
def test_derived_goal_context_cannot_bypass_source_consent(world, monkeypatch, tmp_path, private):
    from uuid import uuid4
    from nexus.core.plugins.session_context import authority_allowed
    from nexus.core.memory.automatic import AutomaticMemory
    from nexus.contracts.records import MemoryPolicyUpdate
    from test_auto_memory import enabled
    app = app_for(world, monkeypatch)
    async def run():
        if private=='revoked_memory': await enabled(world)
        conv,grant,root,goal,_=await ready(app,world,tmp_path)
        async with world.db.transaction() as conn:
            await goals.set_plan(conn,goal,plan())
            if private=='revoked_memory':
                await conn.execute('UPDATE chat_turns SET auto_memory_revision=1 WHERE turn_id=?',(root['turn_id'],))
            else:
                await conn.execute("UPDATE chat_turns SET references_json='[{}]' WHERE turn_id=?",(root['turn_id'],))
            await authority_allowed(conn,goal['goal_id'],current_turn_id=root['turn_id'])
        if private=='revoked_memory':
            await AutomaticMemory(world.db).update(world.owner,MemoryPolicyUpdate(expected_revision=1,enabled=False))
        async with world.db.transaction(write=False) as conn:
            with pytest.raises(AccessDenied,match='AUTONOMY_CONTEXT_CONSENT_REQUIRED'):
                await core_tools.execute(app.state.chat.plugins,conn,world.owner,conv,{'turn_id':str(uuid4())},grant,'builtin.goal','get_goal',{})
            assert (await goals.snapshot(conn,goal['goal_id']))['plan']  # Owner UI can still inspect its records.
        await lifecycle.tick(app.state.chat.plugins)
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn,'SELECT state FROM plugin_authorities'))[0]=='paused'
            assert (await one(conn,'SELECT count(*) FROM plugin_wakeups'))[0]==0
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['cycle', 'unknown', 'duplicate'])
def test_invalid_graph_rejected(kind):
    value = plan().model_dump(mode='json')
    if kind == 'cycle': value['steps'][0]['depends_on'] = ['read']
    if kind == 'unknown': value['steps'][0]['criteria'] = ['missing']
    if kind == 'duplicate': value['steps'].append(value['steps'][0])
    with pytest.raises(ValidationError): GoalPlan.model_validate(value)
