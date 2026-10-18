"""Chat naming and native function proposals keep the existing execution boundary."""

import asyncio
import json
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic import SecretStr

from nexus.contracts.chat import ChatActionDecision, ChatSubmission, ConversationCreate, ConversationUpdate
from nexus.core.config import TrustedNode
from nexus.core.models.chat import DeepSeekChatProvider
from nexus.core.models.config import ModelConfig
from nexus.core.models.deepseek import ProviderFailure
from nexus.core.models.providers import ModelReply
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import timestamp, utc_now
from nexus.core.storage.tasks import Conflict
from test_chat import Stream, app_for, rows, sse
from test_node_link import grant


SYSTEM={"intent":"get_system_info","arguments":{}}
DIRECTORY={"intent":"list_directory","arguments":{"root_id":"workspace","limit":2}}


class ReplyProvider:
    def __init__(self, proposal=None, *, block=False):
        self.calls=[]
        self.started=asyncio.Event()
        self.release=asyncio.Event()
        self.block=block
        self.reply=ModelReply("建议已准备好。",100,20,True,title="工作区概览",proposal=proposal)

    async def invoke(self,messages,receive,**options):
        self.calls.append((messages,options))
        await receive(self.reply.content[:2])
        self.started.set()
        if self.block:await self.release.wait()
        return self.reply


def setup_chat(world,monkeypatch,proposal=None,**options):
    grant(world);grant(world,"filesystem.list")
    provider=ReplyProvider(proposal,**options)
    app=app_for(world,monkeypatch,provider)
    app.state.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset({"system.info@1","filesystem.list@1"}),roots=frozenset({"workspace"}))
    return app.state.chat,provider


async def submit(chat,world,*,conversation=None,**changes):
    conversation=conversation or await chat.create(world.owner,ConversationCreate(request_id=uuid4()))
    history=await chat.history(world.owner,conversation["conversation_id"])
    value=dict(request_id=uuid4(),expected_revision=history["conversation"]["revision"],text="帮我检查一下工作区",allow_model=True,allow_tools=True,node_id=world.node_id)
    value.update(changes)
    turn=await chat.submit(world.owner,conversation["conversation_id"],ChatSubmission(**value))
    return conversation,turn


def decision(turn,kind="confirm"):
    return ChatActionDecision(proposal_digest=turn["proposal_digest"],decision=kind)


@pytest.mark.parametrize("manual",[False,True])
def test_first_reply_generates_title_once_and_preserves_concurrent_manual_name(world,monkeypatch,manual):
    chat,provider=setup_chat(world,monkeypatch,block=True)
    async def run():
        conv,turn=await submit(chat,world)
        worker=asyncio.create_task(chat.process_one())
        await asyncio.wait_for(provider.started.wait(),2)
        if manual:await chat.update(world.owner,conv["conversation_id"],ConversationUpdate(title="我的名称"))
        provider.release.set();await worker
        first=await chat.history(world.owner,conv["conversation_id"])
        assert first["conversation"]["title"]==("我的名称" if manual else "工作区概览")
        assert provider.calls[0][1]["generate_title"] is True
        await submit(chat,world,conversation=conv,text="继续说")
        await chat.process_one()
        assert provider.calls[1][1]["generate_title"] is False
        assert len(await rows(world))==2
    asyncio.run(run())


@pytest.mark.parametrize("proposal",[SYSTEM,DIRECTORY])
def test_action_requires_confirmation_is_atomic_and_idempotent(world,monkeypatch,proposal):
    chat,provider=setup_chat(world,monkeypatch,proposal)
    async def run():
        _,turn=await submit(chat,world)
        await chat.process_one()
        turn=await chat.get(world.owner,UUID(turn["turn_id"]))
        assert turn["proposal"]==proposal and turn["action_status"]=="proposed"
        assert await rows(world,"tasks")==[]
        result=await chat.decide_action(world.owner,UUID(turn["turn_id"]),decision(turn))
        again=await chat.decide_action(world.owner,UUID(turn["turn_id"]),decision(turn))
        assert result["task"].task_id==again["task"].task_id
        assert result["task"].status==("ready" if proposal==SYSTEM else "awaiting_approval")
        assert len(await rows(world,"tasks"))==1 and await rows(world,"task_attempts")==[]
        linked=await chat.get(world.owner,UUID(turn["turn_id"]))
        assert linked["task_id"]==str(result["task"].task_id) and linked["action_status"]=="submitted"
        assert (await rows(world))[0]["task_id"]==linked["task_id"] and len(provider.calls)==1
    asyncio.run(run())


@pytest.mark.parametrize("mode",["digest","reject","pause","cancel_all","policy","scope","expired","archive","node_identity"])
def test_stale_or_unapproved_actions_never_create_tasks(world,monkeypatch,mode):
    chat,provider=setup_chat(world,monkeypatch,SYSTEM)
    async def run():
        conv,turn=await submit(chat,world);await chat.process_one()
        turn=await chat.get(world.owner,UUID(turn["turn_id"]))
        value=decision(turn);principal=world.owner
        if mode=="digest":value=ChatActionDecision(proposal_digest="0"*64,decision="confirm")
        elif mode=="reject":await chat.decide_action(world.owner,UUID(turn["turn_id"]),decision(turn,"reject"))
        elif mode in {"pause","cancel_all"}:await chat.harness.control(world.owner,mode,uuid4())
        elif mode=="policy":await world.security.set_grant(world.node_id,"system.info",enabled=False)
        elif mode=="scope":chat.harness.link.config.nodes[world.node_id]=TrustedNode(tools=frozenset())
        elif mode=="expired":
            async with world.db.transaction() as conn:await conn.execute("UPDATE chat_turns SET expires_at=? WHERE turn_id=?",(timestamp(utc_now()),turn["turn_id"]))
        elif mode=="archive":await chat.update(world.owner,conv["conversation_id"],ConversationUpdate(archived=True))
        elif mode=="node_identity":principal=world.node
        with pytest.raises((AccessDenied,Conflict)):
            await chat.decide_action(principal,UUID(turn["turn_id"]),value)
        assert await rows(world,"tasks")==[] and len(provider.calls)==1
    asyncio.run(run())


def test_task_link_failure_rolls_back_task_and_ledger_association(world,monkeypatch):
    chat,_=setup_chat(world,monkeypatch,SYSTEM)
    async def run():
        _,turn=await submit(chat,world);await chat.process_one()
        turn=await chat.get(world.owner,UUID(turn["turn_id"]))
        async with world.db.transaction() as conn:
            await conn.execute("CREATE TRIGGER fail_chat_link BEFORE UPDATE OF task_id ON chat_turns WHEN NEW.task_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'test link failure'); END")
        import sqlite3
        with pytest.raises(sqlite3.DatabaseError):await chat.decide_action(world.owner,UUID(turn["turn_id"]),decision(turn))
        assert await rows(world,"tasks")==[] and (await rows(world))[0]["task_id"] is None
        async with world.db.transaction() as conn:await conn.execute("DROP TRIGGER fail_chat_link")
        assert (await chat.decide_action(world.owner,UUID(turn["turn_id"]),decision(turn)))["task"]
    asyncio.run(run())


def test_unrequested_action_fails_without_losing_known_cost_or_stalling_worker(world,monkeypatch):
    chat,_=setup_chat(world,monkeypatch,SYSTEM)
    async def run():
        _,turn=await submit(chat,world,allow_tools=False,node_id=None);await chat.process_one()
        result=await chat.get(world.owner,UUID(turn["turn_id"]))
        assert result["status"]=="failed" and result["proposal"] is None and result["cost_status"]=="calculated"
        assert chat.active is None and await rows(world,"tasks")==[]
    asyncio.run(run())


def function_delta(*,index=0,name="",arguments="",identifier=None):
    call={"index":index,"function":{"name":name,"arguments":arguments}}
    if identifier is not None:call.update(id=identifier,type="function")
    return sse(delta={"tool_calls":[call]})


def test_native_function_arguments_and_title_stream_are_bounded_and_separate():
    parts=[];captured=[]
    chunks=[sse('{"title":"系'),sse('统信息"}\n'),function_delta(name="get_system_info",arguments="{",identifier="call-one"),function_delta(arguments="}"),sse(finish="tool_calls",usage={"prompt_tokens":80,"completion_tokens":10}),b"data: [DONE]\n\n"]
    async def handle(request):
        captured.append(json.loads(request.content))
        return httpx2.Response(200,headers={"content-type":"text/event-stream"},stream=Stream(chunks))
    async def run():
        async def receive(part):parts.append(part)
        provider=DeepSeekChatProvider(SecretStr("synthetic"),ModelConfig(),transport=httpx2.MockTransport(handle))
        reply=await provider.invoke([{"role":"user","content":"看看系统"}],receive,generate_title=True,allow_tools=True)
        assert reply.proposal==SYSTEM and reply.title=="系统信息" and reply.content=="" and reply.complete
    asyncio.run(run())
    assert parts==[] and len(captured[0]["tools"])==2
    assert captured[0]["tool_choice"]=="auto" and "parallel_tool_calls" not in captured[0]


@pytest.mark.parametrize("chunks",[
    [function_delta(index=1,name="get_system_info",arguments="{}",identifier="other")],
    [function_delta(name="shell",arguments="{}",identifier="one"),sse(finish="tool_calls",usage={"prompt_tokens":5,"completion_tokens":5}),b"data: [DONE]\n\n"],
    [function_delta(name="list_directory",arguments='{"root_id":"C:/","limit":2}',identifier="one"),sse(finish="tool_calls",usage={"prompt_tokens":5,"completion_tokens":5}),b"data: [DONE]\n\n"],
    [function_delta(name="get_system_info",arguments="{}",identifier="one"),function_delta(name="get_system_info",arguments="{}",identifier="two")],
    [function_delta(name="get_system_info",arguments="x"*2049,identifier="one")],
])
def test_invalid_native_calls_fail_closed(chunks):
    async def handle(request):return httpx2.Response(200,headers={"content-type":"text/event-stream"},stream=Stream(chunks))
    async def run():
        async def receive(part):pass
        provider=DeepSeekChatProvider(SecretStr("synthetic"),ModelConfig(),transport=httpx2.MockTransport(handle))
        with pytest.raises(ProviderFailure):await provider.invoke([{"role":"user","content":"test"}],receive,allow_tools=True)
    asyncio.run(run())


def test_v7_migration_preserves_manual_title_turns_and_call_ledger(tmp_path):
    import hashlib
    import sqlite3
    from contextlib import closing
    from importlib.resources import files
    from nexus.core.storage.database import Database
    path=tmp_path/"core.sqlite3";now=timestamp(utc_now());owner=str(uuid4());cid=str(uuid4());tid=str(uuid4())
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)")
        for resource in sorted(files("nexus.core.storage").joinpath("migrations").iterdir(),key=lambda p:p.name):
            if resource.name.endswith(".sql") and int(resource.name[:3])<=7:
                source=resource.read_text(encoding="utf-8");conn.executescript(source)
                conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)",(int(resource.name[:3]),hashlib.sha256(source.encode()).hexdigest(),now));conn.commit()
        conn.execute("INSERT INTO identities VALUES(?, 'owner', 'synthetic-hash', NULL, ?)",(owner,now))
        conn.execute("INSERT INTO conversations VALUES(?,?,?,'手动会话名称',0,3,?)",(cid,owner,str(uuid4()),now))
        request=json.dumps({'include_context':True,'text':'旧消息','allow_model':True})
        conn.execute("INSERT INTO chat_turns VALUES(?,?,?,?,?,?,?,?,?,0,'旧回答',2,'completed',NULL,?,?)",(tid,cid,owner,str(uuid4()),request,'旧消息','[]','hash',0,now,now))
        conn.execute("INSERT INTO model_calls(call_id,provider,model,cost_microunits,cost_status,created_at,chat_turn_id) VALUES(?,'deepseek','historical',10,'calculated',?,?)",(str(uuid4()),now,tid));conn.commit()
        snapshots={table:conn.execute('SELECT * FROM '+table).fetchall() for table in ('conversations','chat_turns','model_calls')}
    asyncio.run(Database(path).initialize())
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('SELECT title,auto_title FROM conversations').fetchone()==('手动会话名称',0)
        for table,expected in snapshots.items():
            actual=conn.execute('SELECT * FROM '+table).fetchall()
            assert [r[:len(expected[0])] for r in actual]==expected
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
    backups=list(tmp_path.glob('core.sqlite3.before-v8-*.sqlite3'));assert len(backups)==1
    with closing(sqlite3.connect(backups[0])) as conn:assert conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
