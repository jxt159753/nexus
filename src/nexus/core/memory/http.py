"""Session-authenticated explicit record management."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from nexus.contracts.base import StrictContract
from nexus.contracts.records import MemoryCreate, ProjectCreate
from nexus.core.harness.http import body
from nexus.core.memory.service import Records
from nexus.core.security.http import SecurityHTTP
from nexus.core.storage.tasks import Conflict


class RecordsHTTP:
    def __init__(self, guards: SecurityHTTP):
        self.guards = guards
        self.records = Records(guards.service.database)
        self.router = APIRouter()
        self.router.add_api_route("/api/v1/projects", self.create_project, methods=["POST"])
        self.router.add_api_route("/api/v1/memories", self.save, methods=["POST"])
        self.router.add_api_route("/api/v1/memories/{memory_id}/retract", self.retract, methods=["POST"])
        self.router.add_api_route("/api/v1/records/{kind}", self.page, methods=["GET"])
        self.router.add_api_route("/api/v1/memories", self.memories, methods=["GET"])
        self.router.add_api_route("/api/v1/models/usage", self.usage, methods=["GET"])

    async def create_project(self, request: Request) -> dict:
        principal = await self.guards.owner_http(request)
        return {"project": await self.records.create_project(principal, await body(request, ProjectCreate))}

    async def save(self, request: Request) -> dict:
        principal = await self.guards.owner_http(request)
        value = await body(request, MemoryCreate, max_bytes=16384)
        try:
            return {"memory": await self.records.save(principal, value)}
        except Conflict:
            raise
        except ValueError:
            raise HTTPException(400, detail="INVALID_MESSAGE") from None

    async def retract(self, memory_id: UUID, request: Request) -> dict:
        principal = await self.guards.owner_http(request)
        await body(request, StrictContract)
        return {"memory": await self.records.retract(principal, memory_id)}

    async def page(self, kind: Literal["projects", "memories", "routes"], request: Request,
                   after: int = Query(0, ge=0, le=2**63-1), limit: int = Query(20, ge=1, le=100),
                   history: bool = False, project_id: UUID | None = None) -> dict:
        principal = await self.guards.owner_http(request)
        return await self.records.page(principal, kind, after=after, limit=limit, history=history, project_id=project_id)

    async def memories(self, request: Request, after: int = Query(0, ge=0, le=2**63-1), limit: int = Query(20, ge=1, le=100), history: bool = False, project_id: UUID | None = None) -> dict:
        return await self.page("memories", request, after, limit, history, project_id)

    async def usage(self, request: Request) -> dict:
        return await self.records.model_usage(await self.guards.owner_http(request))
