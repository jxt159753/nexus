"""Public web evidence, bounded transport, shared costs and real cancellation."""

import asyncio
import json

import httpx2
import pytest
from pydantic import SecretStr, ValidationError

from nexus.contracts.web import WebFetchArguments, WebSearchArguments
from nexus.core.models.web import DeepSeekWebProvider
from nexus.core.storage.database import one
from test_chat import rows
from test_chat_skills import app_for
from test_chat_work import basic
from test_workspace_work import send_workspace


@pytest.mark.parametrize("url",["file:///etc/passwd","http://localhost/","http://127.0.0.1/","https://10.0.0.1/","http://[::1]/","https://host.local/","https://user:secret@example.com/","https://example.com:9999/","https://example.com/\nCookie: secret"])
def test_public_url_scope(url):
    with pytest.raises(ValidationError):
        WebFetchArguments(url=url)


def test_query_and_arguments_are_bounded():
    for value in [{"query":" "},{"query":"q"*129},{"query":"valid","count":6},{"query":"q","timeout":600}]:
        with pytest.raises(ValidationError):
            WebSearchArguments.model_validate(value)


def test_official_endpoints_payloads_and_bounded_page():
    seen=[]
    async def handle(request):
        seen.append((str(request.url),json.loads(request.content)))
        assert request.headers["authorization"]=="Bearer synthetic"
        if request.url.path.endswith("search"):
            return httpx2.Response(200,json={"results":[{"url":"https://example.com/","title":"Official","snippet":"Summary","content":"must not leak","ignored":"x"}]})
        return httpx2.Response(200,json={"title":"Page","content":"资料"*5000})
    async def run():
        provider=DeepSeekWebProvider(SecretStr("synthetic"),transport=httpx2.MockTransport(handle))
        search=await provider.invoke("web_search",{"query":"query","count":2})
        page=await provider.invoke("web_fetch",{"url":"https://example.com/"})
        assert search.billable and search.data["sources"][0]=={"url":"https://example.com/","title":"Official","snippet":"Summary"}
        assert page.billable and page.data["truncated"] and len(page.data["content"].encode())<=8192
        assert seen==[("https://api.deepseek.com/v1/tools/search",{"query":"query","count":2,"need_content":False}),("https://api.deepseek.com/v1/tools/fetch",{"url":"https://example.com/"})]
    asyncio.run(run())


@pytest.mark.parametrize("status,payload,billable,error",[(200,{"results":[]},False,None),(402,{},False,"WEB_QUOTA_EXCEEDED"),(429,{},False,"WEB_RATE_LIMITED"),(302,{},False,"WEB_REQUEST_FAILED"),(200,{"unexpected":"body"},None,"WEB_RESPONSE_INVALID")])
def test_free_empty_errors_and_ambiguous_response(status,payload,billable,error):
    async def handle(request):return httpx2.Response(status,json=payload,headers={"location":"https://untrusted.example/"})
    async def run():
        reply=await DeepSeekWebProvider(SecretStr("synthetic"),transport=httpx2.MockTransport(handle)).invoke("web_search",{"query":"q"})
        assert reply.billable is billable and reply.error==error
    asyncio.run(run())


def configured(app,handler):
    chat=app.state.chat
    chat.config.web_enabled=True
    chat.work.web.provider=DeepSeekWebProvider(SecretStr("synthetic"),transport=httpx2.MockTransport(handler))
    return chat


async def drain(chat,world,turn_id):
    for _ in range(30):
        await chat.process_one()
        await asyncio.sleep(.01)
        turn=await chat.get(world.owner,turn_id)
        if turn["status"] in {"completed","failed","stopped"} and not turn["work_status"]:
            return turn
    raise AssertionError("web work did not finish")


def test_search_fetch_then_answer_share_budget_and_persist_evidence(world,monkeypatch):
    app=app_for(world,monkeypatch);seen=[]
    async def handle(request):
        seen.append(request.url.path)
        return httpx2.Response(200,json={"results":[{"url":"https://example.com/","title":"Official","snippet":"Search evidence"}]} if request.url.path.endswith("search") else {"title":"Page","content":"External evidence"})
    async def run():
        await basic(app,world)
        chat=configured(app,handle)
        turn=await send_workspace(app,world,[{"intent":"web_search","arguments":{"query":"public information"}},{"intent":"web_fetch","arguments":{"url":"https://example.com/"}}])
        done=await drain(chat,world,turn["turn_id"])
        assert done["status"]=="completed" and done["model_call_count"]==3 and done["web_call_count"]==2
        calls=await rows(world)
        assert sum(call["cost_microunits"] for call in calls)==done["cost_microunits"]
        assert [call["cost_microunits"] for call in calls if call["web_step_id"]]==[750,750]
        assert all(call["work_root_turn_id"]==turn["turn_id"] for call in calls)
        assert [step["result"]["verification"] for step in done["work_steps"]]==["provider_response_checked"]*2
        assert [step["status"] for step in done["work_steps"]]==["succeeded"]*2
        assert seen==["/v1/tools/search","/v1/tools/fetch"] and not await rows(world,"tasks")
    asyncio.run(run())


@pytest.mark.parametrize("stop",["user","pause","revoke","restart"])
def test_cancel_pause_revoke_and_restart_do_not_repeat_paid_request(world,monkeypatch,stop):
    app=app_for(world,monkeypatch)
    async def run():
        entered,closed=asyncio.Event(),asyncio.Event();seen=[]
        async def handle(request):
            seen.append(request.url.path);entered.set()
            try:await asyncio.Event().wait()
            finally:closed.set()
        await basic(app,world)
        chat=configured(app,handle)
        turn=await send_workspace(app,world,[{"intent":"web_search","arguments":{"query":"q"}}])
        await chat.process_one();await chat.process_one()
        await asyncio.wait_for(entered.wait(),2)
        if stop=="user":await chat.cancel(world.owner,turn["turn_id"])
        elif stop=="pause":
            async with world.db.transaction() as conn:await conn.execute("UPDATE system_state SET paused=1 WHERE singleton=1")
        elif stop=="revoke":await chat.work.revoke(world.owner,turn["conversation_id"])
        else:await chat.work.web.close();await chat.recover()
        for _ in range(4):await chat.process_one();await asyncio.sleep(.01)
        assert closed.is_set() and seen==["/v1/tools/search"]
        calls=await rows(world)
        web=next(call for call in calls if call["web_step_id"])
        assert web["cost_status"]=="unknown" and web["reserved_microunits"]==750 and web["cost_microunits"] is None
        assert len(calls)==2
        await chat.work.web.close()
    asyncio.run(run())


def test_web_reservation_obeys_daily_shared_limit_before_network(world,monkeypatch):
    app=app_for(world,monkeypatch);seen=[]
    async def handle(request):seen.append(request);return httpx2.Response(200,json={"results":[]})
    async def run():
        await basic(app,world)
        chat=configured(app,handle)
        turn=await send_workspace(app,world,[{"intent":"web_search","arguments":{"query":"q"}}])
        await chat.process_one()
        chat.config.daily_cost_microunits=749
        done=await drain(chat,world,turn["turn_id"])
        assert done["error_code"]=="BUDGET_EXCEEDED" and not seen
        assert len(await rows(world))==1
    asyncio.run(run())


def test_context_limit_keeps_known_web_charge_and_chat_worker_usable(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def handle(request):return httpx2.Response(200,json={"title":"Page","content":"actual content"})
    async def run():
        await basic(app,world)
        chat=configured(app,handle)
        turn=await send_workspace(app,world,[{"intent":"web_fetch","arguments":{"url":"https://example.com/"}}])
        await chat.process_one();await chat.process_one()
        async with world.db.transaction() as conn:
            row=await one(conn,"SELECT work_messages_json FROM chat_turns WHERE turn_id=?",(turn["turn_id"],))
            messages=json.loads(row[0]);messages[0]["content"]="x"*28000
            await conn.execute("UPDATE chat_turns SET work_messages_json=? WHERE turn_id=?",(json.dumps(messages),turn["turn_id"]))
        done=await drain(chat,world,turn["turn_id"])
        assert done["error_code"]=="MODEL_INPUT_LIMIT" and not chat.failed
        web=next(call for call in await rows(world) if call["web_step_id"])
        assert web["cost_status"]=="calculated" and web["cost_microunits"]==750
        second=await send_workspace(app,world,[])
        await chat.process_one()
        assert (await chat.get(world.owner,second["turn_id"]))["status"]=="completed"
    asyncio.run(run())


def test_excessively_nested_web_json_is_a_bounded_unknown_response():
    async def handle(request):return httpx2.Response(200,content=b'['*2000+b'0'+b']'*2000)
    async def run():
        reply=await DeepSeekWebProvider(SecretStr("synthetic"),transport=httpx2.MockTransport(handle)).invoke("web_search",{"query":"q"})
        assert reply.error=="WEB_RESPONSE_INVALID" and reply.billable is None
    asyncio.run(run())
