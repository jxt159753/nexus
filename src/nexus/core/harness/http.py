"""Owner HTTP entry points for the S4 backend loop; browser WS is S6."""

import asyncio
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import ValidationError

from nexus.contracts.base import StrictContract
from nexus.contracts.task import TaskRequest
from nexus.core.harness.service import Harness
from nexus.core.security.http import SecurityHTTP
from nexus.core.storage.database import one


class Submission(StrictContract):
    node_id: UUID
    request: TaskRequest


class Decision(StrictContract):
    decision: Literal["approve", "reject"]


async def body(request: Request, model):
    value = bytearray()
    try:
        async with asyncio.timeout(2):
            async for chunk in request.stream():
                if len(value) + len(chunk) > 4096:
                    raise HTTPException(413, detail="INVALID_MESSAGE")
                value.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, detail="INVALID_MESSAGE") from None
    try:
        return model.model_validate_json(value)
    except ValidationError:
        if model is Submission:
            try:
                parsed = json.loads(value)
                intent = parsed.get("request", {}).get("intent")
                if isinstance(intent, str) and intent not in {"get_system_info", "list_directory"}:
                    raise HTTPException(400, detail="UNSUPPORTED_TASK")
            except (ValueError, AttributeError):
                pass
        raise HTTPException(400, detail="INVALID_MESSAGE") from None


class HarnessHTTP:
    def __init__(self, harness: Harness, guards: SecurityHTTP):
        self.harness, self.guards = harness, guards
        self.router = APIRouter()
        self.router.add_api_route("/api/v1/tasks", self.submit, methods=["POST"])
        self.router.add_api_route("/api/v1/tasks", self.list_tasks, methods=["GET"])
        self.router.add_api_route("/api/v1/tasks/{task_id}", self.detail, methods=["GET"])
        self.router.add_api_route("/api/v1/approvals/{approval_id}", self.decide, methods=["POST"])

    async def submit(self, request: Request, response: Response) -> dict:
        principal = await self.guards.owner_http(request)
        submitted = await body(request, Submission)
        task = await self.harness.submit(principal, submitted.node_id, submitted.request)
        response.headers["Cache-Control"] = "no-store"
        return {"task": task}

    async def list_tasks(self, request: Request, response: Response, after: int = Query(0, ge=0, le=2**63-1), limit: int = Query(20, ge=1, le=100)) -> dict:
        principal = await self.guards.owner_http(request)
        tasks, cursor = await self.harness.tasks.list(principal.identity_id, after=after, limit=limit)
        response.headers["Cache-Control"] = "no-store"
        return {"tasks": tasks, "next_after": str(cursor)}

    async def detail(self, task_id: UUID, request: Request, response: Response) -> dict:
        principal = await self.guards.owner_http(request)
        result = await self.harness.executions.detail(task_id, principal.identity_id)
        response.headers["Cache-Control"] = "no-store"
        return result

    async def decide(self, approval_id: UUID, request: Request, response: Response) -> dict:
        principal = await self.guards.owner_http(request)
        decision = await body(request, Decision)
        async with self.harness.security.database.transaction(write=False) as conn:
            row = await one(conn, "SELECT node_id FROM approvals WHERE approval_id=? AND owner_id=?", (str(approval_id), str(principal.identity_id)))
            if row is None:
                raise HTTPException(404, detail="NOT_FOUND")
        task = await self.harness.security.decide(approval_id, principal, decision.decision, self.harness.scope(UUID(row[0])))
        self.harness.wake.set()
        response.headers["Cache-Control"] = "no-store"
        return {"task": task}
