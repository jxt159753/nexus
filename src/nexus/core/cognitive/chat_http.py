"""Same-origin owner chat API; Core supplies context and model roles."""

from uuid import UUID

from fastapi import APIRouter, Query, Request

from nexus.contracts.base import StrictContract
from nexus.contracts.records import MemorySelection
from nexus.contracts.work import WorkGrantCreate, WorkPermissionReply
from nexus.core.cognitive import work_catalog
from nexus.contracts.chat import ChatSteering, ChatResume, ChatReferences, ChatActionDecision, ChatPlanPreview, ChatResultConsent, ChatResultPreview, ChatSubmission, ConversationCreate, ConversationUpdate
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
            ("/turns/{turn_id}/result-preview", "POST", self.preview_result),
            ("/turns/{turn_id}/result-consent", "POST", self.consent_result),
            ("/skills", "GET", self.skills),
            ("/turns/{turn_id}/plan-preview", "POST", self.preview_plan),
            ("/conversations/{conversation_id}/memory-preview", "POST", self.memory_preview),
            ("/work-catalog", "GET", self.work_catalog),
            ("/conversations/{conversation_id}/work-grant", "POST", self.work_grant),
            ("/conversations/{conversation_id}/work-grant", "DELETE", self.work_revoke),
            ("/turns/{turn_id}/permission", "POST", self.work_permission),
            ("/turns/{turn_id}/journal", "GET", self.journal),
            ("/turns/{turn_id}/steer", "POST", self.steer),
            ("/turns/{turn_id}/resume", "POST", self.resume),
            ("/conversations/{conversation_id}/reference-preview", "POST", self.reference_preview),
        ]:
            self.router.add_api_route(path, handler, methods=[method])

    async def journal(self,turn_id: UUID,request: Request):
        return await self.service.journal.detail(await self.guards.owner_http(request),turn_id)

    async def steer(self,turn_id: UUID,request: Request):
        await self.service.journal.steer(await self.guards.owner_http(request),turn_id,await body(request,ChatSteering))
        return {"queued":True}

    async def resume(self,turn_id: UUID,request: Request):
        await self.service.journal.resume(await self.guards.owner_http(request),turn_id,await body(request,ChatResume))
        return {"resumed":True}

    async def reference_preview(self,conversation_id: UUID,request: Request):
        return await self.service.journal.preview_references(await self.guards.owner_http(request),conversation_id,await body(request,ChatReferences))

    async def work_catalog(self, request: Request, node_id: UUID | None = None):
        principal=await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            if node_id is None:
                from nexus.core.cognitive.node_routing import directory
                return await directory(self.service,conn,principal)
            return await work_catalog.catalog(self.service,conn,principal,node_id)

    async def work_grant(self,conversation_id: UUID,request: Request):
        return {"grant":await self.service.work.grant(await self.guards.owner_http(request),conversation_id,await body(request,WorkGrantCreate))}

    async def work_revoke(self,conversation_id: UUID,request: Request):
        principal=await self.guards.owner_http(request)
        await body(request,StrictContract)
        await self.service.work.revoke(principal,conversation_id)
        return {"revoked":True}

    async def work_permission(self,turn_id: UUID,request: Request):
        await self.service.work.answer(await self.guards.owner_http(request),turn_id,await body(request,WorkPermissionReply))
        return {"turn":await self.service.get(await self.guards.owner_http(request),turn_id)}

    async def create(self, request: Request):
        principal = await self.guards.owner_http(request)
        return {"conversation": await self.service.create(principal, await body(request, ConversationCreate))}

    async def skills(self, request: Request, node_id: UUID):
        return await self.service.skill_catalog(await self.guards.owner_http(request), node_id)

    async def preview_plan(self, turn_id: UUID, request: Request):
        return await self.service.preview_plan(await self.guards.owner_http(request), turn_id, await body(request, ChatPlanPreview, max_bytes=16384))

    async def page(self, request: Request, before: int = Query(2**63-1, ge=1, le=2**63-1), archived: bool = False, project_id: UUID | None = None, project_only: bool = False):
        return {"conversations": await self.service.page(await self.guards.owner_http(request), before, archived, project_id, project_only)}

    async def memory_preview(self, conversation_id: UUID, request: Request):
        return await self.service.memory_preview(await self.guards.owner_http(request), conversation_id, await body(request, MemorySelection))

    async def update(self, conversation_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        return {"conversation": await self.service.update(principal, conversation_id, await body(request, ConversationUpdate))}

    async def history(self, conversation_id: UUID, request: Request, before: int = Query(2**63-1, ge=1, le=2**63-1)):
        return await self.service.history(await self.guards.owner_http(request), conversation_id, before)

    async def submit(self, conversation_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        value = await body(request, ChatSubmission, max_bytes=16384)
        if value.device_client_id:
            async with self.service.database.transaction(write=False) as conn:
                await self.service.devices.verify(conn, principal, value.device_client_id, request.headers.get("X-Nexus-Device-Proof"))
        return {"turn": await self.service.submit(principal, conversation_id, value, verified_client_id=value.device_client_id)}

    async def get(self, turn_id: UUID, request: Request):
        return {"turn": await self.service.get(await self.guards.owner_http(request), turn_id)}

    async def stop(self, turn_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        await body(request, StrictContract)
        return {"turn": await self.service.cancel(principal, turn_id)}

    async def decide_action(self,turn_id: UUID,request: Request):
        principal=await self.guards.owner_http(request)
        return await self.service.decide_action(principal,turn_id,await body(request,ChatActionDecision))

    async def preview_result(self, turn_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        return {"preview": await self.service.preview_result(principal, turn_id, await body(request, ChatResultPreview))}

    async def consent_result(self, turn_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        return {"turn": await self.service.consent_result(principal, turn_id, await body(request, ChatResultConsent))}
