"""Owner session, origin and CSRF protect all natural-language actions."""

from uuid import UUID

from fastapi import APIRouter, Query, Request

from nexus.contracts.base import StrictContract
from nexus.contracts.language import LanguageSubmission
from nexus.core.harness.http import body


class LanguageHTTP:
    def __init__(self, service, guards):
        self.service, self.guards = service, guards
        self.router = APIRouter()
        self.router.add_api_route("/api/v1/language", self.submit, methods=["POST"])
        self.router.add_api_route("/api/v1/language", self.page, methods=["GET"])
        self.router.add_api_route("/api/v1/language/{language_id}", self.get, methods=["GET"])
        self.router.add_api_route("/api/v1/language/{language_id}/confirm", self.confirm, methods=["POST"])
        self.router.add_api_route("/api/v1/language/{language_id}/cancel", self.cancel, methods=["POST"])

    async def submit(self, request: Request):
        principal = await self.guards.owner_http(request)
        return {"request": await self.service.submit(principal, await body(request, LanguageSubmission, max_bytes=16384))}

    async def page(self, request: Request, before: int = Query(2**63-1, ge=1, le=2**63-1)):
        return {"requests": await self.service.list(await self.guards.owner_http(request), before)}

    async def get(self, language_id: UUID, request: Request):
        return {"request": await self.service.get(await self.guards.owner_http(request), language_id)}

    async def confirm(self, language_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        await body(request, StrictContract)
        return await self.service.confirm(principal, language_id)

    async def cancel(self, language_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        await body(request, StrictContract)
        return {"request": await self.service.cancel(principal, language_id)}
