"""Owner-authenticated client deployment and independent evaluation receipts."""

from uuid import UUID
from fastapi import APIRouter, Query, Request

from nexus.contracts.ui_extensions import UIExtensionPolicy, UIExtensionDecision, UIClient, UITestReport, UIRuntimeReport
from nexus.core.harness.http import body
from nexus.core.plugins import ui_extension_management as management


class UIExtensionHTTP:
    def __init__(self, service, guards):
        self.service, self.guards = service, guards
        self.router = APIRouter(prefix='/api/v1/ui-extensions/conversations/{conversation_id}')
        self.router.add_api_route('', self.inspect, methods=['GET'])
        self.router.add_api_route('/policy', self.configure, methods=['PUT'])
        self.router.add_api_route('/poll', self.poll, methods=['POST'])
        self.router.add_api_route('/versions/{version_id}/test-report', self.test_report, methods=['POST'])
        self.router.add_api_route('/runtime-report', self.runtime_report, methods=['POST'])
        self.router.add_api_route('/management', self.management, methods=['GET'])
        self.router.add_api_route('/decision', self.decision, methods=['POST'])

    async def management(self, conversation_id: UUID, request: Request,
                         before_version: int = Query(2**63-1, ge=1, le=2**63-1),
                         before_event: int = Query(2**63-1, ge=1, le=2**63-1)):
        principal = await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            return await management.detail(self.service, conn, principal, conversation_id,
                                           before_version=before_version, before_event=before_event)

    async def decision(self, conversation_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        return await management.decide(self.service, principal, conversation_id, await body(request, UIExtensionDecision))

    async def inspect(self, conversation_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            return await self.service.inspect(conn, principal, conversation_id)

    async def configure(self, conversation_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        return await self.service.configure(principal, conversation_id, await body(request, UIExtensionPolicy))

    async def poll(self, conversation_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        return await self.service.client_poll(principal, conversation_id, await body(request, UIClient))

    async def test_report(self, conversation_id: UUID, version_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        return await self.service.test_report(principal, conversation_id, version_id, await body(request, UITestReport, max_bytes=24576))

    async def runtime_report(self, conversation_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        return await self.service.runtime_report(principal, conversation_id, await body(request, UIRuntimeReport))
