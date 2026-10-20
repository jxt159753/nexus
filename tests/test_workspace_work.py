"""Default workspace operations and provider batches keep original safety bounds."""
import asyncio,json
from uuid import UUID,uuid4

import httpx2
import pytest
from pydantic import SecretStr

from nexus.contracts.chat import ChatSubmission,ConversationCreate
from nexus.core.cognitive import work_catalog
from nexus.core.models.chat import DeepSeekChatProvider
from nexus.core.models.config import ModelConfig
from nexus.core.models.deepseek import ProviderFailure
from nexus.core.models.providers import ModelReply
from test_chat import Stream,sse,rows
from test_chat_work import basic,file_task,WorkProvider
from test_chat_skills import app_for
from test_extensions_m4 import finish_task


async def send_workspace(app,world,actions):
    chat=app.state.chat;chat.provider=WorkProvider(actions)
    conversation=await chat.create(world.owner,ConversationCreate(request_id=uuid4()))
    async with world.db.transaction(write=False) as conn:
        directory=await work_catalog.catalog(chat,conn,world.owner,world.node_id)
    submission=ChatSubmission(request_id=uuid4(),expected_revision=conversation["revision"],text="列出工作区文件并且把电脑信息写入一个新的txt",allow_model=True,allow_tools=True,node_id=world.node_id,workspace_work=True,work_digest=directory["work_digest"],allow_work_results=True)
    turn=await chat.submit(world.owner,conversation["conversation_id"],submission)
    assert (await chat.submit(world.owner,conversation["conversation_id"],submission))["turn_id"]==turn["turn_id"]
    return turn


def test_configured_workspace_read_write_has_no_extra_grant_or_write_decision(world,monkeypatch,tmp_path):
    app=app_for(world,monkeypatch);folder=tmp_path/"workspace";folder.mkdir()
    async def run():
        await basic(app,world)
        turn=await send_workspace(app,world,[{"intent":"write_file","arguments":{"root_id":"workspace","path":"computer.txt","content":"System information\n"}}])
        chat=app.state.chat
        await chat.process_one();await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        assert current["work_status"]=="waiting" and current["work_steps"][0]["preview_task_id"]
        await file_task(app,world,current["work_steps"][0]["preview_task_id"],folder)
        await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        assert current["work_status"]!="permission" and current["work_steps"][0]["task_id"]
        await file_task(app,world,current["work_steps"][0]["task_id"],folder)
        await chat.process_one()
        assert (await chat.get(world.owner,turn["turn_id"]))["status"]=="completed"
        assert (folder/"computer.txt").read_text()=="System information\n"
        assert not await rows(world,"chat_work_answers")
        assert len(await rows(world,"chat_work_grants"))==1
    asyncio.run(run())


@pytest.mark.parametrize("legacy",[False,True])
def test_real_sse_multi_call_interleaving_and_known_usage(legacy):
    calls=[{"index":0,"id":"system","type":"function","function":{"name":"get_system_info","arguments":"{"}},
           {"index":1,"id":"directory","type":"function","function":{"name":"list_directory","arguments":"{"}}]
    chunks=[sse("先读取两项信息"),sse(delta={"tool_calls":calls}),
        sse(delta={"tool_calls":[{"index":1,"id":None,"type":None,"function":{"name":None,"arguments":'"root_id":"workspace","limit":2}'}},{"index":0,"function":{"arguments":"}"}}]}),
        sse(finish="tool_calls",usage={"prompt_tokens":100,"completion_tokens":20}),b"data: [DONE]\n\n"]
    async def handle(request):return httpx2.Response(200,headers={"content-type":"text/event-stream"},stream=Stream(chunks))
    async def run():
        from nexus.core.cognitive.work_catalog import BUILTINS
        catalog=[{"name":name,"parameters":args.model_json_schema(),"description":description} for name,(_,args,description) in BUILTINS.items()]
        provider=DeepSeekChatProvider(SecretStr("synthetic"),ModelConfig(),transport=httpx2.MockTransport(handle))
        async def receive(text):pass
        if legacy:
            with pytest.raises(ProviderFailure,match="MODEL_TOOL_CALL_LIMIT") as error:
                await provider.invoke([{"role":"user","content":"two reads"}],receive,allow_tools=True)
            assert error.value.usage==(100,20)
        else:
            reply=await provider.invoke([{"role":"user","content":"two reads"}],receive,allow_tools=True,work_catalog=catalog)
            assert [proposal["intent"] for proposal,_ in reply.work_calls]==["get_system_info","list_directory"]
            assert reply.input_tokens==100 and len(reply.work_calls)==2
    asyncio.run(run())


def test_batch_is_validated_before_any_effect_and_waits_for_all_results(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        await basic(app,world)
        world.scope=app.state.harness.scope(world.node_id)
        chat=app.state.chat
        turn=await send_workspace(app,world,[])
        calls=[]
        for index,proposal in enumerate([{"intent":"get_system_info","arguments":{}},{"intent":"list_directory","arguments":{"root_id":"workspace","limit":100}}]):
            calls.append((proposal,{"id":str(index),"type":"function","function":{"name":proposal["intent"],"arguments":json.dumps(proposal["arguments"])}}))
        class Batch:
            def __init__(self):self.count=0
            async def invoke(self,messages,receive,**options):
                self.count+=1
                if self.count==1:return ModelReply("",100,20,True,proposal=calls[0][0],native_tool_call=calls[0][1],work_calls=tuple(calls))
                assert len(messages[-3]["tool_calls"])==2
                assert [message["tool_call_id"] for message in messages[-2:]]==["0","1"]
                return ModelReply("两项结果已收到",100,20,True)
        chat.provider=Batch()
        await chat.process_one();await chat.process_one()
        current=await chat.get(world.owner,turn["turn_id"])
        assert current["work_steps"][0]["task_id"] and not current["work_steps"][1]["task_id"]
        await finish_task(world,UUID(current["work_steps"][0]["task_id"]))
        await chat.process_one();await chat.process_one()
        assert chat.provider.count==1
        current=await chat.get(world.owner,turn["turn_id"])
        await finish_task(world,UUID(current["work_steps"][1]["task_id"]))
        await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="completed" and done["model_call_count"]==2
    asyncio.run(run())


@pytest.mark.parametrize("invalid",["path","duplicate_id","too_many"])
def test_invalid_batch_never_creates_partial_steps_or_tasks(world,monkeypatch,invalid):
    app=app_for(world,monkeypatch)
    async def run():
        await basic(app,world)
        chat=app.state.chat
        turn=await send_workspace(app,world,[])
        proposals=[{"intent":"get_system_info","arguments":{}},{"intent":"read_file","arguments":{"root_id":"workspace","path":"../private.txt" if invalid=="path" else "notes.txt"}}]
        if invalid=="too_many":proposals*=3
        calls=tuple((proposal,{"id":"same" if invalid=="duplicate_id" else str(index),"type":"function","function":{"name":proposal["intent"],"arguments":json.dumps(proposal["arguments"])}}) for index,proposal in enumerate(proposals))
        class Invalid:
            async def invoke(self,*args,**options):return ModelReply("",100,20,True,proposal=calls[0][0],native_tool_call=calls[0][1],work_calls=calls)
        chat.provider=Invalid()
        await chat.process_one();await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="failed" and done["cost_status"]=="calculated"
        assert not await rows(world,"tasks") and not await rows(world,"chat_work_steps")
    asyncio.run(run())


@pytest.mark.parametrize("known",[False,True])
def test_exact_provider_error_is_not_masked_and_known_usage_is_settled(world,monkeypatch,known):
    app=app_for(world,monkeypatch)
    async def run():
        await basic(app,world)
        turn=await send_workspace(app,world,[])
        class Broken:
            async def invoke(self,*args,**kw):raise ProviderFailure("MODEL_TOOL_CALL_LIMIT",usage=(100,20) if known else None)
        app.state.chat.provider=Broken()
        await app.state.chat.process_one()
        done=await app.state.chat.get(world.owner,turn["turn_id"])
        assert done["error_code"]=="MODEL_TOOL_CALL_LIMIT" and done["status"]=="failed"
        assert done["cost_status"]==("calculated" if known else "unknown")
        assert not await rows(world,"tasks")
    asyncio.run(run())
