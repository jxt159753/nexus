"""Session-authenticated explicit record management."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from nexus.contracts.base import StrictContract
from nexus.contracts.records import MemoryCreate, MemoryPolicyUpdate, ProjectCreate, TodoCreate, TodoUpdate
from nexus.core.memory.automatic import AutomaticMemory
from nexus.core.memory.projects import Projects
from nexus.core.harness.http import body
from nexus.core.memory.service import Records
from nexus.core.security.http import SecurityHTTP
from nexus.core.storage.tasks import Conflict


class RecordsHTTP:
    def __init__(self, guards: SecurityHTTP):
        self.guards = guards
        self.records = Records(guards.service.database)
        self.automatic = AutomaticMemory(guards.service.database)
        self.projects = Projects(guards.service.database)
        self.router = APIRouter()
        self.router.add_api_route("/api/v1/projects", self.create_project, methods=["POST"])
        self.router.add_api_route("/api/v1/memories", self.save, methods=["POST"])
        self.router.add_api_route("/api/v1/memories/policy", self.memory_policy, methods=["GET"])
        self.router.add_api_route("/api/v1/memories/policy", self.update_memory_policy, methods=["PATCH"])
        self.router.add_api_route("/api/v1/memories/{memory_id}/retract", self.retract, methods=["POST"])
        self.router.add_api_route("/api/v1/records/{kind}", self.page, methods=["GET"])
        self.router.add_api_route("/api/v1/memories", self.memories, methods=["GET"])
        self.router.add_api_route("/api/v1/models/usage", self.usage, methods=["GET"])
        self.router.add_api_route("/api/v1/projects/{project_id}", self.overview, methods=["GET"])
        self.router.add_api_route("/api/v1/projects/{project_id}/todos", self.todo_create, methods=["POST"])
        self.router.add_api_route("/api/v1/projects/{project_id}/todos/{todo_id}", self.todo_update, methods=["PATCH"])
        self.router.add_api_route("/api/v1/projects/{project_id}/todos/{todo_id}/history", self.todo_history, methods=["GET"])

    async def memory_policy(self, request: Request):
        return {"policy": await self.automatic.get(await self.guards.owner_http(request))}

    async def update_memory_policy(self, request: Request):
        principal = await self.guards.owner_http(request)
        return {"policy": await self.automatic.update(principal, await body(request, MemoryPolicyUpdate))}

    async def overview(self, project_id: UUID, request: Request, before: int = Query(2**63-1, ge=1, le=2**63-1)):
        return await self.projects.overview(await self.guards.owner_http(request), project_id, before=before)

    async def todo_create(self, project_id: UUID, request: Request):
        return {"todo": await self.projects.create_todo(await self.guards.owner_http(request), project_id, await body(request, TodoCreate))}

    async def todo_update(self, project_id: UUID, todo_id: UUID, request: Request):
        return {"todo": await self.projects.update_todo(await self.guards.owner_http(request), project_id, todo_id, await body(request, TodoUpdate))}

    async def todo_history(self, project_id: UUID, todo_id: UUID, request: Request):
        return {"events": await self.projects.history(await self.guards.owner_http(request), project_id, todo_id)}

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
        value = await self.records.model_usage(await self.guards.owner_http(request))
        language = request.app.state.language
        value["enabled"] = language.config.enabled and not language.failed and not language.harness.failed
        value["chat_enabled"] = value["enabled"] and language.config.chat_enabled and not request.app.state.chat.failed
        value["web_enabled"] = value["chat_enabled"] and language.config.web_enabled
        value["cost_limits_enabled"] = language.config.cost_limits_enabled
        value["call_limits_enabled"] = language.config.call_limits_enabled
        value["chat_max_output_tokens"] = language.config.chat_output_limit()
        return value
