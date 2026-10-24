"""Authenticated upload/download API; opaque files are always attachments."""

import asyncio
import base64
import json
from typing import Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import ValidationError

from nexus.contracts.attachments import AttachmentSelection, MAX_UPLOAD_BASE64, MAX_UPLOAD_BYTES, UploadJSON, UploadMetadata
from nexus.contracts.base import StrictContract
from nexus.contracts.images import ImageSnapshot
from nexus.core.harness.http import body


class TransferResponse(Response):
    """Keep the transfer slot until completion or client disconnection."""
    def __init__(self, *args, release, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = release

    async def __call__(self, scope, receive, send):
        try:
            async with asyncio.timeout(30):
                await send({"type":"http.response.start","status":self.status_code,"headers":self.raw_headers})
                for offset in range(0,len(self.body),65536):
                    await send({"type":"http.response.body","body":self.body[offset:offset+65536],"more_body":True})
                await send({"type":"http.response.body","body":b"","more_body":False})
        finally:
            self.body = b""
            self.release()


class AttachmentHTTP:
    def __init__(self, chat, guards):
        self.service, self.guards = chat.attachments, guards
        self.router = APIRouter(prefix="/api/v1/chat/conversations/{conversation_id}/attachments")
        for path,method,handler in [("","POST",self.upload),("","GET",self.page),("/model-preview","POST",self.selection),
                                    ("/{attachment_id}/preview","GET",self.preview),("/{attachment_id}/content","GET",self.content),
                                    ("/{attachment_id}/image","GET",self.image),("/{attachment_id}","DELETE",self.remove)]:
            self.router.add_api_route(path,handler,methods=[method])

    async def upload(self, conversation_id: UUID, request: Request, encoding: Literal["raw","base64"] = "raw",
                     request_id: UUID | None = None, filename: str | None = Query(None,max_length=200), media_type: str = "application/octet-stream"):
        principal = await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.conversation(conn,principal,conversation_id)
        if request.headers.get("content-encoding","identity") != "identity":
            raise HTTPException(415,detail="UPLOAD_ENCODING_UNSUPPORTED")
        if self.service.incoming.locked():
            raise HTTPException(429,detail="UPLOAD_BUSY")
        limit = MAX_UPLOAD_BYTES if encoding == "raw" else MAX_UPLOAD_BASE64+2048
        try:
            if int(request.headers.get("content-length","0")) > limit:
                raise HTTPException(413,detail="UPLOAD_TOO_LARGE")
        except ValueError:
            raise HTTPException(400,detail="INVALID_MESSAGE") from None
        async with self.service.incoming:
            raw = bytearray()
            try:
                async with asyncio.timeout(30):
                    async for chunk in request.stream():
                        if len(raw)+len(chunk) > limit:
                            raise HTTPException(413,detail="UPLOAD_TOO_LARGE")
                        raw.extend(chunk)
            except TimeoutError:
                raise HTTPException(408,detail="UPLOAD_TIMEOUT") from None
            try:
                if encoding == "base64":
                    value = UploadJSON.model_validate_json(raw)
                    metadata = UploadMetadata.model_validate(value.model_dump(exclude={"data_base64"}))
                    data = base64.b64decode(value.data_base64,validate=True)
                    if len(data)>MAX_UPLOAD_BYTES:
                        raise HTTPException(413,detail="UPLOAD_TOO_LARGE")
                    if base64.b64encode(data).decode("ascii")!=value.data_base64:
                        raise ValueError("invalid bounded base64")
                else:
                    metadata = UploadMetadata(request_id=request_id,filename=filename,media_type=media_type)
                    data = bytes(raw)
            except (ValidationError,ValueError):
                raise HTTPException(400,detail="INVALID_UPLOAD") from None
            return {"attachment":await self.service.upload(principal,conversation_id,metadata,data)}

    async def page(self, conversation_id: UUID, request: Request, before: int = Query(2**63-1,ge=1,le=2**63-1)):
        return await self.service.page(await self.guards.owner_http(request),conversation_id,before)

    async def preview(self, conversation_id: UUID, attachment_id: UUID, request: Request):
        row = await self.service.get(await self.guards.owner_http(request),conversation_id,attachment_id)
        return {"attachment":self.service.public(row),"preview":json.loads(row["preview_json"])}

    async def content(self, conversation_id: UUID, attachment_id: UUID, request: Request, encoding: Literal["raw","base64"] = "raw"):
        principal = await self.guards.owner_http(request)
        if self.service.incoming.locked():
            raise HTTPException(429,detail="UPLOAD_BUSY")
        await self.service.incoming.acquire()
        try:
            row = await self.service.get(principal,conversation_id,attachment_id,content=True)
            if encoding == "base64":
                data = json.dumps({"attachment":self.service.public(row),"data_base64":base64.b64encode(row["content"]).decode("ascii")},ensure_ascii=False)
                return TransferResponse(data,media_type="application/json",release=self.service.incoming.release)
            return TransferResponse(row["content"],media_type="application/octet-stream",release=self.service.incoming.release,headers={
                "Content-Disposition":"attachment; filename*=UTF-8''"+quote(row["filename"],safe=""),
                "X-Content-Type-Options":"nosniff","Cache-Control":"no-store"})
        except BaseException:
            self.service.incoming.release()
            raise

    async def image(self, conversation_id: UUID, attachment_id: UUID, request: Request):
        row = await self.service.get(await self.guards.owner_http(request),conversation_id,attachment_id)
        preview = json.loads(row["preview_json"])
        if preview["kind"] != "image":
            raise HTTPException(404,detail="IMAGE_PREVIEW_UNAVAILABLE")
        image = ImageSnapshot.model_validate(preview["snapshot"]).image
        return Response(base64.b64decode(image.data_base64),media_type=image.media_type,
                        headers={"Content-Disposition":"inline; filename=preview","Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})

    async def selection(self, conversation_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        value = await body(request,AttachmentSelection)
        return await self.service.preview(principal,conversation_id,value.attachment_ids)

    async def remove(self, conversation_id: UUID, attachment_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        await body(request,StrictContract)
        await self.service.remove(principal,conversation_id,attachment_id)
        return {"removed":True}
