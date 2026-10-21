"""Owner HTTP entry points sharing the browser's Harness authority."""

import asyncio
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import ValidationError

from nexus.contracts.base import StrictContract
from nexus.contracts.messages import ControlRequest
from nexus.contracts.task import TaskRequest
from nexus.core.harness.service import Harness
from nexus.core.security.http import SecurityHTTP


class Submission(StrictContract):
    node_id: UUID
    request: TaskRequest


class Decision(StrictContract):
    decision: Literal["approve", "reject"]


async def body(request: Request, model, *, max_bytes: int = 4096):
    value = bytearray()
    try:
        async with asyncio.timeout(2):
            async for chunk in request.stream():
                if len(value) + len(chunk) > max_bytes:
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
                if isinstance(intent, str) and intent not in {"get_system_info", "list_directory", "read_file", "find_files", "preview_file_write", "write_file", "run_command", "read_file_lines", "glob_files", "search_file_content", "preview_file_edit", "edit_file", "observe_target", "interact_target"}:
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
        self.router.add_api_route("/api/v1/tasks/{task_id}/cancel", self.cancel, methods=["POST"])
        self.router.add_api_route("/api/v1/control", self.control_state, methods=["GET"])
        self.router.add_api_route("/api/v1/control/{kind}", self.control, methods=["POST"])

    async def cancel(self, task_id: UUID, request: Request, response: Response) -> dict:
        principal = await self.guards.owner_http(request)
        await body(request, StrictContract)
        response.headers["Cache-Control"] = "no-store"
        return await self.harness.cancel(principal, task_id)

    async def control_state(self, request: Request, response: Response) -> dict:
        await self.guards.owner_http(request)
        response.headers["Cache-Control"] = "no-store"
        return await self.harness.controls.state()

    async def control(self, kind: Literal["pause", "resume", "cancel_all"], request: Request, response: Response) -> dict:
        principal = await self.guards.owner_http(request)
        value = await body(request, ControlRequest)
        response.headers["Cache-Control"] = "no-store"
        return await self.harness.control(principal, kind, value.request_id)

    async def submit(self, request: Request, response: Response) -> dict:
        principal = await self.guards.owner_http(request)
        submitted = await body(request, Submission, max_bytes=32768)
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
        task = await self.harness.decide(principal, approval_id, decision.decision)
        response.headers["Cache-Control"] = "no-store"
        return {"task": task}
