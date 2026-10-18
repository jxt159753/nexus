"""Same-origin owner chat API; Core supplies context and model roles."""

from uuid import UUID

from fastapi import APIRouter, Query, Request

from nexus.contracts.base import StrictContract
from nexus.contracts.chat import ChatActionDecision, ChatSubmission, ConversationCreate, ConversationUpdate
from nexus.core.harness.http import body


class ChatHTTP:
    def __init__(self, service, guards):
        self.service, self.guards = service, guards
        self.router = APIRouter(prefix="/api/v1/chat")
        for path, method, handler in [
            ("/conversations", "POST", self.create), ("/conversations", "GET", self.page),
            ("/conversations/{conversation_id}", "PATCH", self.update),
            ("/conversations/{conversation_id}/turns", "GET", self.history),
            ("/conversations/{conversation_id}/turns", "POST", self.submit),
            ("/turns/{turn_id}", "GET", self.get), ("/turns/{turn_id}/stop", "POST", self.stop),
            ("/turns/{turn_id}/action", "POST", self.decide_action),
        ]:
            self.router.add_api_route(path, handler, methods=[method])

    async def create(self, request: Request):
        principal = await self.guards.owner_http(request)
        return {"conversation": await self.service.create(principal, await body(request, ConversationCreate))}

    async def page(self, request: Request, before: int = Query(2**63-1, ge=1, le=2**63-1), archived: bool = False):
        return {"conversations": await self.service.page(await self.guards.owner_http(request), before, archived)}

    async def update(self, conversation_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        return {"conversation": await self.service.update(principal, conversation_id, await body(request, ConversationUpdate))}

    async def history(self, conversation_id: UUID, request: Request, before: int = Query(2**63-1, ge=1, le=2**63-1)):
        return await self.service.history(await self.guards.owner_http(request), conversation_id, before)

    async def submit(self, conversation_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        return {"turn": await self.service.submit(principal, conversation_id, await body(request, ChatSubmission, max_bytes=16384))}

    async def get(self, turn_id: UUID, request: Request):
        return {"turn": await self.service.get(await self.guards.owner_http(request), turn_id)}

    async def stop(self, turn_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        await body(request, StrictContract)
        return {"turn": await self.service.cancel(principal, turn_id)}

    async def decide_action(self,turn_id: UUID,request: Request):
        principal=await self.guards.owner_http(request)
        return await self.service.decide_action(principal,turn_id,await body(request,ChatActionDecision))
