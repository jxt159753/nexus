"""Catalog evidence and bounded multi-step setup, including final summaries."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from nexus.core.models.chat import build_request
from nexus.core.models.config import ModelConfig
from nexus.core.cognitive.chat_context import fit_work_context, request_size
from nexus.core.storage.database import one
from nexus.plugins.catalog import RELEASES
from test_chat_skills import app_for
from test_chat_work import setup


@pytest.mark.parametrize('actions', [4, 12])
def test_final_summary_retains_capabilities_but_cannot_call_tools(actions):
    config = ModelConfig(chat_work_max_actions=actions)
    catalog = [{'name':'device1__plugin_execute','route_intent':'plugin_execute','route_node_id':'device',
                'description':'execute', 'parameters':{'type':'object'}}]
    request = build_request(config, [{'role':'user','content':'安装 MCP'}], work_catalog=catalog, allow_tools=False, work_limit=0)
    prompt = request['messages'][0]['content']
    assert 'No arbitrary shell' not in prompt
    assert 'exhausted actions do not mean' in prompt
    assert 'Native plugins may provide host shell' in prompt
    assert 'tools' not in request
    with pytest.raises(ValueError): ModelConfig(chat_work_max_actions=17)


def test_context_fitting_accounts_for_auto_memory_in_actual_request():
    config = ModelConfig(chat_input_bytes=32768)
    messages = [{'role':'user','content':'check'}, {'role':'tool','tool_call_id':'call',
                'content':json.dumps({'status':'succeeded','data':{'output':'x'*24000}})}]
    without_memory = request_size(config, messages, work_catalog=[], auto_memory=False)
    config.chat_input_bytes = without_memory
    assert request_size(config, messages, work_catalog=[], auto_memory=True) > without_memory
    fitted = fit_work_context(config, messages, [], auto_memory=True)
    assert request_size(config, fitted, work_catalog=[], auto_memory=True) <= config.chat_input_bytes
    assert fitted[-1]['tool_call_id'] == 'call'
    assert json.loads(fitted[-1]['content'])['context_projection']['truncated']


def test_configured_round_limit_allows_six_steps_without_resetting_grant(world, monkeypatch):
    app = app_for(world, monkeypatch)
    app.state.chat.config.chat_work_max_actions = 6
    async def run():
        chat = app.state.chat
        _, grant, turn = await setup(app, world, [{'intent':'get_node_status','arguments':{}}]*6)
        for _ in range(8): await chat.process_one()
        result = await chat.get(world.owner, turn['turn_id'])
        assert result['status'] == 'completed' and result['model_call_count'] == 7
        assert len(result['work_steps']) == 6
        assert not chat.provider.calls[-1][1]['allow_tools']
        assert chat.provider.calls[-1][1]['work_catalog']
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn,'SELECT used_operations FROM chat_work_grants WHERE grant_id=?',(grant['grant_id'],)))[0] == 6
    asyncio.run(run())


def test_registry_does_not_claim_unconfigured_shell_is_available(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        service = app.state.plugins
        async with world.db.transaction(write=False) as conn:
            assert (await service.execution_status(conn,world.owner_id,world.node_id,RELEASES['builtin.shell']))['status'] == 'node_offline'
            assert (await service.execution_status(conn,world.owner_id,world.node_id,RELEASES['builtin.clock']))['status'] == 'available'
            app.state.harness.link.connections[world.node_id] = SimpleNamespace(
                hello=SimpleNamespace(plugin_tools={}), scope=world.scope)
            result = await service.execution_status(conn,world.owner_id,world.node_id,RELEASES['builtin.shell'])
            assert result['status'] == 'executor_not_enabled' and result['configured_tools'] == []
    asyncio.run(run())
