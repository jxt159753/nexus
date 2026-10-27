"""In-flight evidence is pending, not a reason to end speech or cancel work."""
import asyncio
import json
from uuid import UUID, uuid4

from nexus.contracts.messages import CommandReceipt
from nexus.contracts.voice import VoiceSpeak, VoiceSpeakCompleted
from nexus.core.config import VoiceConfig
from nexus.core.storage.database import utc_now
from nexus.core.storage.execution import ExecutionRepository, RESULT
from nexus.core.voice.chat_bridge import ChatVoiceBridge
from nexus.core.voice.providers import FakeAsrProvider, FakeTtsProvider
from nexus.core.voice.service import VoiceService
from nexus.node.tools.files import read_file
from test_chat_skills import app_for
from test_chat_work import basic, setup
from test_node_routing import RoutedProvider
from test_voice_lifecycle import prepared


async def dispatch(app, world, task_id):
    repo = ExecutionRepository(world.db, world.security, 32768)
    boot = uuid4()
    command = await repo.dispatch(UUID(task_id), world.node, app.state.harness.scope(world.node_id), uuid4(), boot, 10)
    return repo, command, boot


async def receive(repo, world, command, boot):
    await repo.receipt(world.node, boot, CommandReceipt(session_id=command.session_id, task_id=command.task_id,
        attempt_id=command.attempt_id, command_digest=command.command_digest, boot_id=boot))


async def store(repo, world, command, root):
    output = read_file(command.arguments, root)
    result = RESULT.validate_json(json.dumps({**command.model_dump(mode='json', include={
        'session_id','task_id','attempt_id','tool','tool_version','command_digest'}),
        'observed_at':utc_now().isoformat(), 'outcome':'succeeded', 'result':output.model_dump(mode='json')}))
    await repo.store_result(world.node, result)


def test_dispatched_running_and_verifying_work_are_not_unknown(world, monkeypatch, tmp_path):
    app = app_for(world, monkeypatch)
    (tmp_path/'note.txt').write_text('verified content', encoding='utf-8')
    async def run():
        await basic(app, world)
        chat = app.state.chat
        _, _, turn = await setup(app, world, [{'intent':'read_file','arguments':{'root_id':'workspace','path':'note.txt'}}])
        await chat.process_one()
        await chat.process_one()
        value = await chat.get(world.owner, turn['turn_id'])
        task_id = value['work_steps'][0]['task_id']
        repo, command, boot = await dispatch(app, world, task_id)
        async def pending(status):
            task = await world.tasks.get(UUID(task_id), world.owner_id)
            assert task.status == status and task.execution_outcome == 'unknown'
            view = (await chat.get(world.owner, turn['turn_id']))['presentation']
            assert view['phase'] == 'working'
            assert view['final_text'] is None and not view['collapse_process']
        await pending('dispatching')
        await receive(repo, world, command, boot)
        await pending('running')
        await store(repo, world, command, tmp_path)
        await pending('verifying')
        await repo.verify(command.attempt_id)
        await chat.process_one()
        done = await chat.get(world.owner, turn['turn_id'])
        assert done['presentation']['phase'] == 'completed'
        assert done['work_steps'][0]['result']['execution_outcome'] == 'known'
    asyncio.run(run())


def test_actual_recovery_unknown_still_blocks_final_success(world, monkeypatch):
    app = app_for(world, monkeypatch)
    async def run():
        await basic(app, world)
        chat = app.state.chat
        _, _, turn = await setup(app, world, [{'intent':'read_file','arguments':{'root_id':'workspace','path':'note.txt'}}])
        await chat.process_one()
        await chat.process_one()
        step = (await chat.get(world.owner, turn['turn_id']))['work_steps'][0]
        repo, command, _ = await dispatch(app, world, step['task_id'])
        await repo.recovering(command.attempt_id)
        view = (await chat.get(world.owner, turn['turn_id']))['presentation']
        assert view['phase'] == 'unknown' and '尚未确认' in view['final_text']
        assert not view['collapse_process']
        assert len(chat.provider.calls) == 1
    asyncio.run(run())


def test_voice_waits_for_real_task_verification_then_speaks_and_listens(world, monkeypatch, tmp_path):
    app = app_for(world, monkeypatch)
    (tmp_path/'note.txt').write_text('verified content', encoding='utf-8')
    async def run():
        await basic(app, world)
        chat = app.state.chat
        chat.provider = RoutedProvider([(world.node_id, 'read_file', {'root_id':'workspace','path':'note.txt'})])
        bridge = ChatVoiceBridge(chat, world.owner_id)
        service = VoiceService(world.db, VoiceConfig(enabled=True), world.owner_id,
            asr=FakeAsrProvider(), tts=FakeTtsProvider(seconds=.02), streamer=bridge.stream, canceller=bridge.cancel)
        session, sink = await prepared(world, service)
        session.consent = {'allow_tools':True,'allow_work_results':True,'include_context':True}
        try:
            await service.accept_final(session, '读取文件')
            async with asyncio.timeout(5):
                while not session.chat_turn_id:
                    await asyncio.sleep(.01)
            turn_id = session.chat_turn_id
            await chat.process_one()
            await chat.process_one()
            step = (await chat.get(world.owner, turn_id))['work_steps'][0]
            repo, command, boot = await dispatch(app, world, step['task_id'])
            await receive(repo, world, command, boot)
            # Give the real bridge multiple polling opportunities while a Node
            # is still executing. No error speech or automatic stop is valid.
            await asyncio.sleep(.2)
            assert not any(isinstance(m, VoiceSpeak) for m in sink.messages)
            assert session.state == 'thinking' and not session.end_after_reply
            assert not session.response_task.done()
            await store(repo, world, command, tmp_path)
            await asyncio.sleep(.1)
            assert not any(isinstance(m, VoiceSpeak) for m in sink.messages)
            await repo.verify(command.attempt_id)
            await chat.process_one()
            async with asyncio.timeout(5):
                await sink.ended.wait()
            done = await chat.get(world.owner, turn_id)
            spoken = ''.join(m.text for m in sink.messages if isinstance(m, VoiceSpeak))
            assert spoken == done['presentation']['final_text']
            assert done['status'] == 'completed' and done['work_steps'][0]['status'] == 'succeeded'
            assert not session.end_after_reply and session.state == 'speaking'
            await service.playback_ack(session, VoiceSpeakCompleted(type='voice.speak.completed',message_id=uuid4(),sent_at=utc_now(),
                voice_session_id=session.voice_session_id,session_epoch=session.session_epoch,
                utterance_id=session.assistant_utterance_id,generation=session.generation,segments=1,total_ms=20))
            await asyncio.wait_for(session.response_task, 3)
            assert session.state == 'listening' and session.active
            async with world.db.transaction(write=False) as conn:
                assert (await conn.execute_fetchall('SELECT count(*) FROM task_attempts'))[0][0] == 1
            assert len(chat.provider.calls) == 2
        finally:
            session.media_stopped.set()
            await service.close()
    asyncio.run(run())
