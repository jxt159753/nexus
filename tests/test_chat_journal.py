"""Per-exchange replay, ordered queues, next-boundary corrections and recovery."""

import asyncio
import hashlib
import json
from uuid import UUID, uuid4

import pytest

from nexus.contracts.chat import ChatSubmission, ChatSteering, ChatResume, ChatReferences, ConversationCreate
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one
from test_chat import rows
from test_chat_skills import app_for
from test_chat_work import setup, basic, file_task, WorkProvider


def test_exact_requests_and_native_calls_are_retained_per_exchange(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat
        _,_,turn=await setup(app,world,[{"intent":"get_node_status","arguments":{}}])
        await chat.process_one();await chat.process_one()
        log=await chat.journal.detail(world.owner,turn["turn_id"])
        assert len(log["exchanges"])==2
        first,second=log["exchanges"]
        assert first["request_sha256"]==hashlib.sha256(first["request_body"].encode()).hexdigest()
        assert json.loads(first["response_json"])["native_tool_call"]["id"]=="work-call-1"
        assert json.loads(second["request_body"])["messages"][-1]["tool_call_id"]=="work-call-1"
        assert all(e["outcome"]=="completed" for e in log["exchanges"])
        assert "authorization" not in first["request_body"].lower()
    asyncio.run(run())


def test_queued_followup_does_not_overtake_pending_work(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat
        conv,_,turn=await setup(app,world,[{"intent":"get_node_status","arguments":{}}])
        await chat.process_one()
        history=await chat.history(world.owner,conv["conversation_id"])
        follow=await chat.submit(world.owner,conv["conversation_id"],ChatSubmission(request_id=uuid4(),expected_revision=history["conversation"]["revision"],text="后续问题",allow_model=True,enqueue=True))
        assert follow["queue_after_turn_id"]==turn["turn_id"]
        await chat.process_one()
        assert len(chat.provider.calls)==2
        assert (await chat.get(world.owner,follow["turn_id"]))["status"]=="queued"
        await chat.process_one()
        assert (await chat.get(world.owner,follow["turn_id"]))["status"]=="completed"
    asyncio.run(run())


def test_correction_skips_undispatched_call_without_hiding_native_pair(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat
        _,_,turn=await setup(app,world,[{"intent":"get_node_status","arguments":{}}])
        await chat.process_one()
        value=ChatSteering(request_id=uuid4(),text="不要查询，直接说明状态未知",allow_model=True)
        await chat.journal.steer(world.owner,turn["turn_id"],value)
        await chat.journal.steer(world.owner,turn["turn_id"],value)
        await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["work_steps"][0]["result"]["execution_outcome"]=="not_started"
        messages=chat.provider.calls[-1][0]
        assert messages[-2]["tool_call_id"]=="work-call-1"
        assert "不要查询" in messages[-1]["content"]
        assert len((await chat.journal.detail(world.owner,turn["turn_id"]))["controls"])==1
        assert not await rows(world,"tasks")
    asyncio.run(run())


def test_restart_holds_work_and_explicit_resume_uses_original_steps(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat
        _,_,turn=await setup(app,world,[{"intent":"get_node_status","arguments":{}}])
        await chat.process_one();await chat.recover();await chat.process_one()
        held=await chat.get(world.owner,turn["turn_id"])
        assert held["recovery_required"] and len(chat.provider.calls)==1
        await chat.journal.resume(world.owner,turn["turn_id"],ChatResume(request_id=uuid4(),consent=True))
        await chat.process_one()
        assert (await chat.get(world.owner,turn["turn_id"]))["status"]=="completed"
        assert len(chat.provider.calls)==2
    asyncio.run(run())


def test_cross_conversation_reference_requires_preview_digest_and_consent(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat;chat.provider=WorkProvider([])
        source=await chat.create(world.owner,ConversationCreate(request_id=uuid4()))
        turn=await chat.submit(world.owner,source["conversation_id"],ChatSubmission(request_id=uuid4(),expected_revision=source["revision"],text="第一会话资料",allow_model=True))
        await chat.process_one()
        destination=await chat.create(world.owner,ConversationCreate(request_id=uuid4()))
        snapshot=await chat.journal.preview_references(world.owner,destination["conversation_id"],ChatReferences(turn_ids=[UUID(turn["turn_id"])]))
        follow=await chat.submit(world.owner,destination["conversation_id"],ChatSubmission(request_id=uuid4(),expected_revision=destination["revision"],text="引用这段资料",allow_model=True,reference_turn_ids=[UUID(turn["turn_id"])],references_digest=snapshot["references_digest"],allow_reference_model=True))
        await chat.process_one()
        assert (await chat.get(world.owner,follow["turn_id"]))["status"]=="completed"
        assert "第一会话资料" in json.dumps(chat.provider.calls[-1][0],ensure_ascii=False)
    asyncio.run(run())


def test_exact_body_is_committed_before_http_and_matches_sent_bytes(world,monkeypatch):
    import httpx2
    from pydantic import SecretStr
    from nexus.core.models.chat import DeepSeekChatProvider
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat
        async def exchange(request):
            async with world.db.transaction(write=False) as conn:
                stored=await one(conn,"SELECT request_body FROM chat_exchanges WHERE outcome='started'")
                assert stored and stored[0].encode()==request.content
            events=[{"model":"deepseek-flash","choices":[{"index":0,"delta":{"content":'{"title":"请求记录"}\n实际回复'},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":20}}]
            return httpx2.Response(200,content=b"".join(("data: "+json.dumps(event)+"\n\n").encode() for event in events)+b"data: [DONE]\n\n",headers={"Content-Type":"text/event-stream"})
        chat.provider=DeepSeekChatProvider(SecretStr("test-only"),chat.config,transport=httpx2.MockTransport(exchange))
        conv=await chat.create(world.owner,ConversationCreate(request_id=uuid4()))
        turn=await chat.submit(world.owner,conv["conversation_id"],ChatSubmission(request_id=uuid4(),expected_revision=conv["revision"],text="记录请求",allow_model=True))
        await chat.process_one()
        assert (await chat.get(world.owner,turn["turn_id"]))["status"]=="completed"
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            async with world.db.transaction() as conn:
                await conn.execute("UPDATE chat_exchanges SET request_body='{}'")
    asyncio.run(run())


def test_more_than_ten_rounds_get_source_linked_excerpts_without_extra_calls(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat;chat.provider=WorkProvider([])
        conv=await chat.create(world.owner,ConversationCreate(request_id=uuid4()))
        for index in range(12):
            current=await chat.history(world.owner,conv["conversation_id"])
            await chat.submit(world.owner,conv["conversation_id"],ChatSubmission(request_id=uuid4(),expected_revision=current["conversation"]["revision"],text=f"第 {index} 轮资料",allow_model=True))
            await chat.process_one()
            async with world.db.transaction() as conn:
                await conn.execute("UPDATE chat_turns SET created_at='2020-01-01T00:00:00+00:00'")
        current=await chat.history(world.owner,conv["conversation_id"])
        turn=await chat.submit(world.owner,conv["conversation_id"],ChatSubmission(request_id=uuid4(),expected_revision=current["conversation"]["revision"],text="回顾",allow_model=True))
        detail=await chat.get(world.owner,turn["turn_id"])
        assert detail["context_count"]==10 and len(detail["history_summary"])==2
        assert "turn_id" in detail["context_messages"][0]["content"]
        assert len(chat.provider.calls)==12
    asyncio.run(run())


def test_pending_correction_prevents_dispatch_of_an_already_created_task(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        await basic(app,world)
        chat=app.state.chat
        _,_,turn=await setup(app,world,[{"intent":"read_file","arguments":{"root_id":"workspace","path":"note"}}])
        await chat.process_one();await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        task=await world.tasks.get(UUID(current["work_steps"][0]["task_id"]),world.owner_id)
        assert task.status=="ready"
        await chat.journal.steer(world.owner,turn["turn_id"],ChatSteering(request_id=uuid4(),text="不要读取",allow_model=True))
        with pytest.raises(AccessDenied):
            await world.security.prepare_pending(task.task_id,world.node,app.state.harness.scope(world.node_id))
        await chat.process_one()
        assert (await world.tasks.get(task.task_id,world.owner_id)).status=="cancelled"
        assert (await chat.get(world.owner,turn["turn_id"]))["work_steps"][0]["result"]["execution_outcome"]=="not_started"
    asyncio.run(run())


def test_recovery_requires_original_execution_result_before_resume(world,monkeypatch,tmp_path):
    from nexus.core.storage.execution import ExecutionRepository, RESULT
    from nexus.core.storage.database import utc_now
    from nexus.node.tools.files import read_file
    app=app_for(world,monkeypatch)
    (tmp_path/"note").write_bytes(b"evidence")
    async def run():
        await basic(app,world)
        chat=app.state.chat
        _,_,turn=await setup(app,world,[{"intent":"read_file","arguments":{"root_id":"workspace","path":"note"}}])
        await chat.process_one();await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        task=await world.tasks.get(UUID(current["work_steps"][0]["task_id"]),world.owner_id)
        repo=ExecutionRepository(world.db,world.security,32768)
        command=await repo.dispatch(task.task_id,world.node,app.state.harness.scope(world.node_id),uuid4(),uuid4(),10)
        await chat.recover()
        value=ChatResume(request_id=uuid4(),consent=True)
        with pytest.raises(AccessDenied):
            await chat.journal.resume(world.owner,turn["turn_id"],value)
        output=read_file(command.arguments,tmp_path)
        result=RESULT.validate_json(json.dumps({**command.model_dump(mode="json",include={"session_id","task_id","attempt_id","tool","tool_version","command_digest"}),"observed_at":utc_now().isoformat(),"outcome":"succeeded","result":output.model_dump(mode="json")}))
        await repo.store_result(world.node,result);await repo.verify(command.attempt_id)
        await chat.journal.resume(world.owner,turn["turn_id"],value)
        await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="completed"
        assert done["work_steps"][0]["task_id"]==str(task.task_id)
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn,"SELECT count(*) FROM task_attempts"))[0]==1
    asyncio.run(run())


def test_restart_between_tool_result_and_next_model_exchange_requires_resume(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat
        _,_,turn=await setup(app,world,[{"intent":"get_node_status","arguments":{}}])
        await chat.process_one();await chat.work.advance()
        assert (await chat.get(world.owner,turn["turn_id"]))["status"]=="queued"
        await chat.recover();await chat.process_one()
        assert len(chat.provider.calls)==1
        assert (await chat.get(world.owner,turn["turn_id"]))["recovery_required"]==1
        before=await chat.history(world.owner,turn["conversation_id"])
        await chat.recover()
        assert await chat.history(world.owner,turn["conversation_id"])==before
        await chat.journal.resume(world.owner,turn["turn_id"],ChatResume(request_id=uuid4(),consent=True))
        await chat.process_one()
        assert len(chat.provider.calls)==2
    asyncio.run(run())


def test_work_skill_holds_then_resumes_original_run_and_applies_correction_after_operation(world,monkeypatch):
    from test_chat_skills import prepare
    from test_extensions_m4 import finish_task
    app=app_for(world,monkeypatch)
    async def run():
        version=await prepare(app,world)
        chat=app.state.chat
        _,_,turn=await setup(app,world,[{"intent":"run_skill","arguments":{"version_id":version["version_id"],"input":{}}}])
        await chat.process_one();await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"]);run_id=current["work_steps"][0]["skill_run_id"]
        await chat.recover();await app.state.skills.tick()
        assert not await rows(world,"tasks")
        await chat.journal.resume(world.owner,turn["turn_id"],ChatResume(request_id=uuid4(),consent=True))
        await app.state.skills.tick()
        await chat.journal.steer(world.owner,turn["turn_id"],ChatSteering(request_id=uuid4(),text="完成当前固定流程后简要回答",allow_model=True))
        for _ in range(10):
            await app.state.skills.tick()
            tasks=await rows(world,"tasks")
            for task in tasks:
                if task["status"] in {"ready","awaiting_approval"}:
                    await finish_task(world,UUID(task["task_id"]))
            run=await app.state.skills.get(world.owner,UUID(run_id))
            if run["status"]=="succeeded":
                break
        assert run["status"]=="succeeded",run
        await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="completed" and done["work_steps"][0]["skill_run_id"]==run_id
        assert "完成当前固定流程" in chat.provider.calls[-1][0][-1]["content"]
        assert len(await rows(world,"skill_runs"))==1
    asyncio.run(run())


def test_followup_waits_for_cancelled_skill_child_execution_to_stop(world,monkeypatch):
    from test_chat_skills import prepare
    from nexus.core.storage.execution import ExecutionRepository
    app=app_for(world,monkeypatch)
    async def run():
        version=await prepare(app,world)
        chat=app.state.chat
        conv,_,turn=await setup(app,world,[{"intent":"run_skill","arguments":{"version_id":version["version_id"],"input":{}}}])
        await chat.process_one();await chat.process_one();await app.state.skills.tick()
        task=(await rows(world,"tasks"))[0]
        repo=ExecutionRepository(world.db,world.security,32768)
        command=await repo.dispatch(UUID(task["task_id"]),world.node,world.scope,uuid4(),uuid4(),10)
        await chat.cancel(world.owner,turn["turn_id"])
        current=await chat.history(world.owner,conv["conversation_id"])
        follow=await chat.submit(world.owner,conv["conversation_id"],ChatSubmission(request_id=uuid4(),expected_revision=current["conversation"]["revision"],text="后续问题",allow_model=True,enqueue=True))
        await app.state.skills.tick();await chat.process_one()
        assert (await chat.get(world.owner,follow["turn_id"]))["status"]=="queued"
        assert len(chat.provider.calls)==1
        await repo.cancelled(world.node,command.attempt_id)
        await app.state.skills.tick();await chat.process_one()
        assert (await chat.get(world.owner,follow["turn_id"]))["status"]=="completed"
    asyncio.run(run())
