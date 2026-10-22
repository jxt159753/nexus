"""Uploads are durable private data; attaching and model disclosure are separate."""

import asyncio
import base64
import io
import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nexus.contracts.attachments import UploadMetadata
from nexus.contracts.chat import ChatSubmission, ConversationUpdate
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one
from nexus.core.storage.tasks import Conflict
from test_chat import app_for, conversation, FakeChat, send
from test_harness_http import login
from test_node_link import ORIGIN


async def upload(chat,world,conv,raw=b"private file content",filename="notes.txt",request_id=None):
    return await chat.attachments.upload(world.owner,conv["conversation_id"],UploadMetadata(request_id=request_id or uuid4(),filename=filename),raw)


async def attach(chat,world,conv,items,**changes):
    preview = await chat.attachments.preview(world.owner,conv["conversation_id"],[item["attachment_id"] for item in items])
    history = await chat.history(world.owner,conv["conversation_id"])
    value = ChatSubmission(**{**dict(request_id=uuid4(),expected_revision=history["conversation"]["revision"],text="请查看附件",allow_model=True,
        attachment_ids=[UUID(item["attachment_id"]) for item in items],attachments_digest=preview["attachments_digest"],allow_attachment_model=True),**changes})
    return await chat.submit(world.owner,conv["conversation_id"],value),value


@pytest.mark.parametrize("filename,raw,kind",[("image.png",b"\x89PNG\r\n\x1a\ncorrupt","unparsed"),("archive.zip",b"PK\x03\x04\x00","unparsed"),
    ("program.exe",b"MZ\x00\xff","unparsed"),("document.pdf",b"%PDF-1.4 not parsed","unparsed"),("notes.md","你好\n代码".encode(),"text"),("empty.bin",b"","text")])
def test_any_extension_upload_and_exact_download_without_execution(world,monkeypatch,filename,raw,kind):
    app = app_for(world,monkeypatch)
    async def run():
        chat = app.state.chat; conv = await conversation(chat,world)
        item = await upload(chat,world,conv,raw,filename)
        assert item["preview_kind"] == kind
        row = await chat.attachments.get(world.owner,conv["conversation_id"],item["attachment_id"],content=True)
        assert row["content"] == raw and item["size_bytes"] == len(raw)
        assert not chat.provider.requests
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn,"SELECT count(*) FROM model_calls"))[0] == 0
            assert (await one(conn,"SELECT count(*) FROM tasks"))[0] == 0
    asyncio.run(run())


def test_text_and_unparsed_attachments_require_preview_and_per_turn_consent(world,monkeypatch):
    app = app_for(world,monkeypatch,FakeChat(text="附件里的私有总结"))
    async def run():
        chat = app.state.chat; conv = await conversation(chat,world)
        text = await upload(chat,world,conv,b"private-text")
        opaque = await upload(chat,world,conv,b"%PDF-1.4 opaque-private-body","paper.pdf")
        turn,value = await attach(chat,world,conv,[text,opaque])
        assert len(turn["attachments"]) == 2 and turn["attachments"][1]["model_mode"] == "metadata"
        assert (await chat.submit(world.owner,conv["conversation_id"],value))["turn_id"] == turn["turn_id"]
        await chat.process_one()
        sent = json.dumps(chat.provider.requests[-1],ensure_ascii=False)
        assert "private-text" in sent and "paper.pdf" in sent and "opaque-private-body" not in sent and "尚未解析" in sent
        with pytest.raises(AccessDenied,match="ATTACHMENT_IN_USE"):
            await chat.attachments.remove(world.owner,conv["conversation_id"],text["attachment_id"])
        chat.provider.text = "普通回答"
        for index, compact in enumerate((False,True)):
            follow,_ = await send(chat,world,conv["conversation_id"],text="继续",allow_compaction=compact)
            assert follow["context_count"] == index
            await chat.process_one()
            assert "private-text" not in json.dumps(chat.provider.requests[-1])
    asyncio.run(run())


def test_attachment_only_message_has_a_title_and_text_truncation(world,monkeypatch):
    app = app_for(world,monkeypatch)
    async def run():
        chat = app.state.chat; conv = await conversation(chat,world)
        item = await upload(chat,world,conv,"你".encode()*4000,filename="中文.txt")
        preview = await chat.attachments.preview(world.owner,conv["conversation_id"],[item["attachment_id"]])
        assert preview["payloads"][0]["truncated"] and len(preview["payloads"][0]["text"].encode()) <= 8192
        turn,_ = await attach(chat,world,conv,[item],text="")
        assert (await chat.history(world.owner,conv["conversation_id"]))["conversation"]["title"] == "附件：中文.txt"
        await chat.process_one()
        assert (await chat.get(world.owner,turn["turn_id"]))["status"] == "completed"
    asyncio.run(run())


def test_image_upload_preview_and_actual_model_blocks_without_node(world,monkeypatch):
    Image = pytest.importorskip("PIL.Image")
    raw = io.BytesIO(); Image.new("RGBA",(80,40),(255,0,0,0)).save(raw,format="PNG")
    app = app_for(world,monkeypatch,chat_images_enabled=True)
    async def run():
        chat = app.state.chat; conv = await conversation(chat,world)
        item = await upload(chat,world,conv,raw.getvalue(),filename="不依赖后缀.bin")
        assert item["preview_kind"] == "image" and item["media_type"] == "image/png"
        turn,_ = await attach(chat,world,conv,[item])
        assert turn["node_id"] is None
        await chat.process_one()
        blocks = chat.provider.requests[-1][-1]["content"]
        assert blocks[-1]["type"] == "image_url"
        data = base64.b64decode(blocks[-1]["image_url"]["url"].split(",",1)[1])
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == (80,40) and image.getpixel((0,0)) == (255,255,255)
        async with world.db.transaction(write=False) as conn:
            journal = await one(conn,"SELECT request_body FROM chat_exchanges WHERE turn_id=?",(turn["turn_id"],))
            assert "data:image/png;base64," in journal[0]
    asyncio.run(run())


def test_image_disabled_is_metadata_only_and_config_change_invalidates_consent(world,monkeypatch):
    Image = pytest.importorskip("PIL.Image")
    raw=io.BytesIO(); Image.new("RGB",(10,10)).save(raw,format="PNG")
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat; conv=await conversation(chat,world)
        item=await upload(chat,world,conv,raw.getvalue(),"test.png")
        turn,_=await attach(chat,world,conv,[item])
        assert turn["attachments"][0]["model_mode"]=="metadata"
        await chat.process_one()
        assert "image_url" not in json.dumps(chat.provider.requests[-1])
        chat.config.chat_images_enabled=True
        turn,_=await attach(chat,world,conv,[item])
        chat.config.chat_images_enabled=False
        await chat.process_one()
        done=await chat.get(world.owner,turn["turn_id"])
        assert done["status"]=="failed" and done["error_code"]=="ATTACHMENT_UNAVAILABLE"
        assert len(chat.provider.requests)==1
    asyncio.run(run())


def test_upload_idempotency_isolation_remove_and_archive(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat; conv=await conversation(chat,world); other=await conversation(chat,world)
        identifier=uuid4()
        item=await upload(chat,world,conv,request_id=identifier)
        assert await upload(chat,world,conv,request_id=identifier)==item
        with pytest.raises(Conflict):
            await upload(chat,world,conv,b"changed",request_id=identifier)
        with pytest.raises(Conflict):
            await upload(chat,world,other,request_id=identifier)
        with pytest.raises(LookupError):
            await chat.attachments.get(world.owner,other["conversation_id"],item["attachment_id"],content=True)
        with pytest.raises(LookupError):
            await attach(chat,world,other,[item])
        await chat.attachments.remove(world.owner,conv["conversation_id"],item["attachment_id"])
        await chat.attachments.remove(world.owner,conv["conversation_id"],item["attachment_id"])
        with pytest.raises(LookupError):
            await chat.attachments.get(world.owner,conv["conversation_id"],item["attachment_id"],content=True)
        assert (await upload(chat,world,conv,request_id=identifier))["deleted"]
        await chat.update(world.owner,conv["conversation_id"],ConversationUpdate(archived=True))
        with pytest.raises(AccessDenied):
            await upload(chat,world,conv)
    asyncio.run(run())


def test_stale_selection_size_and_quota_fail_without_partial_data(world,monkeypatch):
    from nexus.core.cognitive import attachments
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat; conv=await conversation(chat,world)
        item=await upload(chat,world,conv)
        with pytest.raises(Conflict):
            await attach(chat,world,conv,[item],attachments_digest="a"*64)
        with pytest.raises(ValueError):
            await upload(chat,world,conv,b"x"*(16*1024*1024+1))
        monkeypatch.setattr(attachments,"MAX_TOTAL_BYTES",1)
        with pytest.raises(Conflict,match="BUSY"):
            await upload(chat,world,conv,b"cannot fit")
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn,"SELECT count(*) FROM chat_attachments"))[0]==1
            assert (await one(conn,"SELECT count(*) FROM chat_turns"))[0]==0
    asyncio.run(run())


@pytest.mark.parametrize("changes",[{"allow_attachment_model":False},{"attachments_digest":None},{"attachment_ids":[]},
    {"attachment_ids":[str(uuid4())]*2},{"compact_only":True,"allow_compaction":True,"text":"/compact"}])
def test_attachment_consent_contract(changes):
    args=dict(request_id=uuid4(),expected_revision=1,text="read",attachment_ids=[uuid4()],attachments_digest="a"*64,allow_attachment_model=True)
    with pytest.raises(ValidationError):
        ChatSubmission(**{**args,**changes})


def test_upload_http_raw_json_download_security_and_deletion(world,monkeypatch):
    app=app_for(world,monkeypatch)
    with TestClient(app,base_url=ORIGIN) as client:
        auth=login(client,world)
        conv=client.post("/api/v1/chat/conversations",json={"request_id":str(uuid4())},headers=auth).json()["conversation"]
        base=f"/api/v1/chat/conversations/{conv['conversation_id']}/attachments"
        params={"request_id":str(uuid4()),"filename":"示例.exe"}
        assert client.post(base,params=params,content=b"MZ\x00binary").status_code==403
        response=client.post(base,params=params,content=b"MZ\x00binary",headers=auth)
        assert response.status_code==200,response.text
        item=response.json()["attachment"]
        content=client.get(item["download_url"])
        assert content.content==b"MZ\x00binary" and content.headers["content-type"]=="application/octet-stream"
        assert content.headers["content-disposition"].startswith("attachment;") and content.headers["x-content-type-options"]=="nosniff"
        encoded=client.get(item["download_url"],params={"encoding":"base64"}).json()
        assert base64.b64decode(encoded["data_base64"])==content.content
        upload_json={"request_id":str(uuid4()),"filename":"notes.txt","data_base64":base64.b64encode(b"text").decode()}
        assert client.post(base,params={"encoding":"base64"},json=upload_json,headers=auth).status_code==200
        assert len(client.get(base).json()["attachments"])==2
        assert client.get(base+"/"+item["attachment_id"]+"/image").status_code==404
        assert client.request("DELETE",base+"/"+item["attachment_id"],json={},headers=auth).status_code==200
        assert client.get(item["download_url"]).status_code==404
        client.cookies.clear()
        assert client.get(base).status_code==401
        assert client.get(item["download_url"]).status_code==401


@pytest.mark.parametrize("filename",["../bad","a\\bad","a/b","bad\r\nheader","", ".."])
def test_upload_names_are_display_names_not_paths(filename):
    with pytest.raises(ValidationError):
        UploadMetadata(request_id=uuid4(),filename=filename)


@pytest.mark.parametrize("compact",[False,True])
def test_large_selected_context_stops_before_any_model_reservation(world,monkeypatch,compact):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat; conv=await conversation(chat,world)
        items=[await upload(chat,world,conv,b"a"*8192,filename=f"part-{index}.txt") for index in range(4)]
        if not compact:
            with pytest.raises(AccessDenied,match="MODEL_INPUT_LIMIT"):
                await attach(chat,world,conv,items)
        else:
            turn,_=await attach(chat,world,conv,items,allow_compaction=True)
            await chat.process_one()
            result=await chat.get(world.owner,turn["turn_id"])
            assert result["status"]=="failed" and result["error_code"]=="MODEL_INPUT_LIMIT"
        assert not chat.provider.requests
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn,"SELECT count(*) FROM model_calls"))[0]==0
            assert (await one(conn,"SELECT count(*) FROM chat_attachments"))[0]==4
    asyncio.run(run())


def test_transfer_slot_and_memory_are_released_when_client_disconnects():
    from nexus.core.cognitive.attachment_http import TransferResponse
    calls=[];chunks=[]
    response=TransferResponse(b"x"*200000,release=lambda:calls.append("released"))
    async def send_message(message):
        if message["type"]=="http.response.body":
            chunks.append(message["body"])
            raise OSError("simulated client disconnect")
    async def run():
        with pytest.raises(OSError):
            await response({},None,send_message)
    asyncio.run(run())
    assert calls==["released"] and response.body==b"" and len(chunks[0])==65536


def test_http_rejects_oversized_stream_invalid_base64_and_foreign_origin(world,monkeypatch):
    from nexus.core.cognitive import attachment_http
    app=app_for(world,monkeypatch)
    with TestClient(app,base_url=ORIGIN) as client:
        auth=login(client,world)
        conv=client.post("/api/v1/chat/conversations",json={"request_id":str(uuid4())},headers=auth).json()["conversation"]
        base=f"/api/v1/chat/conversations/{conv['conversation_id']}/attachments"
        metadata={"request_id":str(uuid4()),"filename":"file.bin"}
        monkeypatch.setattr(attachment_http,"MAX_UPLOAD_BYTES",10)
        assert client.post(base,params=metadata,content=b"too many bytes",headers=auth).status_code==413
        assert client.post(base,params=metadata,content=iter([b"12345678",b"12345678"]),headers=auth).status_code==413
        assert client.post(base,params={"encoding":"base64"},json={**metadata,"data_base64":"!!!!"},headers=auth).status_code==400
        assert client.post(base,params={"encoding":"base64"},json={**metadata,"data_base64":base64.b64encode(b"x"*11).decode()},headers=auth).status_code==413
        assert client.post(base,params=metadata,content=b"x",headers={**auth,"origin":"https://other.invalid"}).status_code==403
        assert client.get(base).json()["attachments"]==[]


def test_duplicate_upload_race_keeps_one_blob_and_one_audit(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat; conv=await conversation(chat,world); key=uuid4()
        first,second=await asyncio.gather(upload(chat,world,conv,request_id=key),upload(chat,world,conv,request_id=key))
        assert first==second
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn,"SELECT count(*) FROM chat_attachments"))[0]==1
            assert (await one(conn,"SELECT count(*) FROM audit_events WHERE kind='chat.attachment_uploaded'"))[0]==1
    asyncio.run(run())


def test_maximum_binary_upload_is_preserved_and_reselection_is_explicit(world,monkeypatch):
    app=app_for(world,monkeypatch)
    async def run():
        chat=app.state.chat; conv=await conversation(chat,world)
        raw=b"\x00"*(16*1024*1024)
        item=await upload(chat,world,conv,raw,"max.bin")
        assert (await chat.attachments.get(world.owner,conv["conversation_id"],item["attachment_id"],content=True))["content"]==raw
        for _ in range(2):
            turn,_=await attach(chat,world,conv,[item])
            await chat.process_one()
            assert (await chat.get(world.owner,turn["turn_id"]))["status"]=="completed"
            assert "max.bin" in json.dumps(chat.provider.requests[-1])
    asyncio.run(run())
