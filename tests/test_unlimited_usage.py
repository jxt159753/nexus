"""Owner-disabled quotas preserve evidence, stop controls and identity boundaries."""
import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from nexus.core.models.budget import budget_error
from nexus.core.models.config import ModelConfig
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from test_chat import app_for, conversation, rows, send
from test_chat_work import setup


def test_chat_above_daily_count_and_amounts_keeps_unknown_history(world, monkeypatch):
    app=app_for(world,monkeypatch,call_limits_enabled=False,cost_limits_enabled=False,
                daily_calls=0,task_cost_microunits=0,daily_cost_microunits=0,
                price_valid_until=utc_now().date()-timedelta(days=1))
    chat=app.state.chat
    async def run():
        conv=await conversation(chat,world)
        await send(chat,world,conv['conversation_id']);await chat.process_one()
        template=(await rows(world))[0]
        async with world.db.transaction() as conn:
            for index in range(105):
                item={**template,'call_id':str(uuid4()),'chat_turn_id':None,
                      'cost_microunits':9_000_000,'reserved_microunits':9_000_000,
                      'cost_status':'unknown' if index<5 else 'calculated',
                      'created_at':timestamp(utc_now()-timedelta(days=2)) if index<5 else timestamp(utc_now())}
                await conn.execute('INSERT INTO model_calls('+','.join(item)+') VALUES('+','.join('?' for _ in item)+')',tuple(item.values()))
            assert await budget_error(conn,chat.config,utc_now()) is None
            assert await budget_error(conn,chat.config.model_copy(update={'enabled':False}),utc_now())=='MODEL_DISABLED'
        turn,_=await send(chat,world,conv['conversation_id'])
        await chat.process_one()
        assert (await chat.get(world.owner,turn['turn_id']))['status']=='completed'
        calls=await rows(world)
        assert len(calls)==107 and sum(c['cost_status']=='unknown' for c in calls)==5
        assert not await rows(world,'tasks')
    asyncio.run(run())


@pytest.mark.parametrize('automatic',[False,True])
def test_work_crosses_action_exchange_and_grant_quotas(world,monkeypatch,automatic):
    app=app_for(world,monkeypatch,call_limits_enabled=False,cost_limits_enabled=False,daily_calls=0,
                chat_work_max_actions=1,chat_input_bytes=65536)
    async def run():
        chat=app.state.chat
        actions=[{'intent':'get_node_status','arguments':{}} for _ in range(24)]
        if automatic:
            from test_workspace_work import send_workspace
            turn=await send_workspace(app,world,actions)
        else:
            _,grant,turn=await setup(app,world,actions,max_operations=1)
            assert not grant['operation_limits_enabled']
        for _ in range(32):
            await chat.process_one()
            done=await chat.get(world.owner,turn['turn_id'])
            if done['status']=='completed' and not done['work_status']:break
        assert done['status']=='completed' and len(done['work_steps'])==24
        assert done['model_call_count']==25 and all(s['status']=='succeeded' for s in done['work_steps'])
        async with world.db.transaction(write=False) as conn:
            grant=await one(conn,'SELECT * FROM chat_work_grants WHERE grant_id=?',(done['work_grant_id'],))
            assert grant['used_operations']==24 and not grant['operation_limits_enabled']
        assert not await rows(world,'tasks')
        _,_,pending=await setup(app,world,[{'intent':'get_node_status','arguments':{}}])
        await chat.process_one();await chat.cancel(world.owner,pending['turn_id'])
        await chat.process_one()
        stopped=await chat.get(world.owner,pending['turn_id'])
        assert stopped['status']=='stopped' and stopped['model_call_count']==1
    asyncio.run(run())


def test_goal_round_quota_disabled_but_pause_still_works(world,monkeypatch):
    from nexus.core.plugins import lifecycle
    from test_plugin_autonomy_recovery import goal_ready,control
    from test_chat_skills import app_for as skill_app
    app=skill_app(world,monkeypatch)
    app.state.chat.config.call_limits_enabled=False
    app.state.chat.config.cost_limits_enabled=False
    async def run():
        chat=app.state.chat
        conv,goal,_=await goal_ready(app,world)
        async with world.db.transaction() as conn:
            await conn.execute('UPDATE plugin_goals SET rounds=1000 WHERE goal_id=?',(goal['goal_id'],))
        await control(chat,world.owner,conv['conversation_id'],goal['goal_id'],'paused')
        await control(chat,world.owner,conv['conversation_id'],goal['goal_id'],'active')
        await lifecycle.tick(chat.plugins)
        async with world.db.transaction(write=False) as conn:
            current=await one(conn,'SELECT rounds,status FROM plugin_goals WHERE goal_id=?',(goal['goal_id'],))
            assert tuple(current)==(1001,'active')
        await control(chat,world.owner,conv['conversation_id'],goal['goal_id'],'paused')
        await lifecycle.tick(chat.plugins)
        async with world.db.transaction(write=False) as conn:
            assert tuple(await one(conn,'SELECT rounds,status FROM plugin_goals WHERE goal_id=?',(goal['goal_id'],)))==(1001,'paused')
    asyncio.run(run())


def test_voice_exceeds_usage_call_and_turn_quotas_with_audit(world):
    from nexus.core.config import VoiceConfig
    from nexus.core.voice.providers import FakeAsrProvider,FakeTtsProvider
    from nexus.core.voice.service import VoiceService
    async def run():
        service=VoiceService(world.db,VoiceConfig(enabled=True,max_turns=1,daily_asr_seconds=60,daily_tts_chars=1000),world.owner_id,
                             asr=FakeAsrProvider(),tts=FakeTtsProvider(),model_config=ModelConfig(call_limits_enabled=False,cost_limits_enabled=False))
        session=await service.open_session(world.node,uuid4(),'button')
        try:
            session.calls=[{'kind':'asr','day':utc_now().date().isoformat(),'reserved':600,'status':'unknown'} for _ in range(100)]
            session.turns=100
            await service.begin_listening(session)
            assert session.active and len(session.calls)==101
            index=await service._reserve(session,'tts',100_000)
            await service._settle(session,index,known=False,observed=0)
            assert len(session.calls)==102 and session.calls[index]['status']=='unknown'
            async with world.db.transaction(write=False) as conn:
                assert '100000' in (await one(conn,'SELECT metrics_json FROM voice_sessions WHERE voice_session_id=?',(str(session.voice_session_id),)))[0]
            await service.stop(session,'user')
            with pytest.raises(AccessDenied):await service._reserve(session,'asr',1)
        finally:await service.close()
    asyncio.run(run())
