"""Owner HTTP endpoints for the self-extension lifecycle."""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request

from nexus.contracts.base import StrictContract
from nexus.contracts.extensions import (
    ExtensionGenerateConsent,
    ExtensionRequestCreate,
)
from nexus.core.harness.http import body
from pydantic import Field, StrictStr, model_validator


class ApprovalInput(StrictContract):
    decision: Literal["approve", "reject"]
    artifact_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    test_id: UUID | None = None


class DisableInput(StrictContract):
    reason: StrictStr | None = Field(default=None, max_length=200)


class RunCreate(StrictContract):
    version_id: UUID
    node_id: UUID
    input: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self):
        import json
        if len(json.dumps(self.input, ensure_ascii=False).encode("utf-8")) > 16384:
            raise ValueError("Skill input exceeds 16 KiB")
        return self


class ExtensionHTTP:
    def __init__(self, service, guards):
        self.service, self.guards = service, guards
        self.router = APIRouter()
        for path, method, handler in [
            ("/api/v1/extensions", "GET", self.state),
            ("/api/v1/extensions/requests", "POST", self.create_request),
            ("/api/v1/extensions/requests", "GET", self.list_requests),
            ("/api/v1/extensions/requests/{request_id}", "GET", self.get_request),
            ("/api/v1/extensions/requests/{request_id}/generate", "POST", self.generate),
            ("/api/v1/extensions/versions", "GET", self.list_versions),
            ("/api/v1/extensions/versions/{version_id}", "GET", self.get_version),
            ("/api/v1/extensions/versions/{version_id}/test", "POST", self.test),
            ("/api/v1/extensions/versions/{version_id}/approve", "POST", self.approve),
            ("/api/v1/extensions/versions/{version_id}/enable", "POST", self.enable),
            ("/api/v1/extensions/versions/{version_id}/disable", "POST", self.disable),
            ("/api/v1/extensions/runs", "POST", self.start_run),
            ("/api/v1/extensions/runs", "GET", self.list_runs),
            ("/api/v1/extensions/runs/{run_id}", "GET", self.get_run),
            ("/api/v1/extensions/runs/{run_id}/cancel", "POST", self.cancel_run),
            ("/api/v1/extensions/sandbox", "GET", self.sandbox),
        ]:
            self.router.add_api_route(path, handler, methods=[method])

    async def state(self, request: Request):
        await self.guards.owner_http(request)
        return {"extension": self.service.readiness()}

    async def create_request(self, request: Request):
        principal = await self.guards.owner_http(request)
        return {"request": await self.service.create(principal, await body(request, ExtensionRequestCreate, max_bytes=16384))}

    async def list_requests(self, request: Request, before: int = Query(2**63 - 1, ge=1, le=2**63 - 1)):
        return {"requests": await self.service.list_requests(await self.guards.owner_http(request), before)}

    async def get_request(self, request_id: UUID, request: Request):
        return {"request": await self.service.get_request(await self.guards.owner_http(request), request_id)}

    async def generate(self, request_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        return {"request": await self.service.generate(principal, request_id, await body(request, ExtensionGenerateConsent))}

    async def list_versions(self, request: Request, capability_id: str | None = Query(None, max_length=70),
                            before: int = Query(2**63 - 1, ge=1, le=2**63 - 1)):
        return {"versions": await self.service.list_versions(await self.guards.owner_http(request), capability_id=capability_id, before=before)}

    async def get_version(self, version_id: UUID, request: Request):
        return {"version": await self.service.get_version(await self.guards.owner_http(request), version_id)}

    async def test(self, version_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        await body(request, StrictContract)
        return {"version": await self.service.test_version(principal, version_id)}

    async def approve(self, version_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        value = await body(request, ApprovalInput)
        return {"version": await self.service.approve(principal, version_id, value.decision, value.artifact_digest, value.test_id)}

    async def enable(self, version_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        await body(request, StrictContract)
        return {"version": await self.service.enable(principal, version_id)}

    async def disable(self, version_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        value = await body(request, DisableInput)
        return {"version": await self.service.disable(principal, version_id, value.reason or "owner disabled")}

    async def start_run(self, request: Request):
        principal = await self.guards.owner_http(request)
        value = await body(request, RunCreate, max_bytes=16384)
        return {"run": await self.service.skills.start(principal, value.version_id, value.node_id, value.input)}

    async def list_runs(self, request: Request, before: int = Query(2**63 - 1, ge=1, le=2**63 - 1)):
        return {"runs": await self.service.skills.list(await self.guards.owner_http(request), before)}

    async def get_run(self, run_id: UUID, request: Request):
        return {"run": await self.service.skills.get(await self.guards.owner_http(request), run_id)}

    async def cancel_run(self, run_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        await body(request, StrictContract)
        return {"run": await self.service.skills.cancel(principal, run_id)}

    async def sandbox(self, request: Request):
        await self.guards.owner_http(request)
        values = []
        for node_id, policy in self.service.harness.link.config.sandbox_nodes.items():
            status = self.service.sandbox.status(node_id) if self.service.sandbox is not None else {"available": False, "reason": "sandbox_not_configured"}
            values.append({"node_id": str(node_id), "runtime": policy.runtime,
                           "image_digest": policy.image_digest, "status": status})
        return {"sandbox_nodes": values}
