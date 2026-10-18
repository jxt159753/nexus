"""Single Core: authenticated transport, durable Harness and owner API."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from importlib.resources import files

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from nexus.core.config import CoreConfig
from nexus.core.harness.http import HarnessHTTP
from nexus.core.harness.service import Harness
from nexus.core.link.nodes import NodeLink
from nexus.core.link.clients import ClientLink
from nexus.core.memory.http import RecordsHTTP
from nexus.core.models.providers import DisabledDecisionModel, DisabledProvider
from nexus.core.security.http import SecurityHTTP, TransportPolicy
from nexus.core.security.service import SecurityService
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import Database
from nexus.core.storage.tasks import Conflict
from nexus.locking import DataDirectoryLock


def create_app(config: CoreConfig) -> FastAPI:
    config = CoreConfig.model_validate_json(config.model_dump_json())
    database = Database(config.data_dir.resolve() / "core.sqlite3")
    guards = SecurityHTTP(SecurityService(database), TransportPolicy(config.origins, config.local_http))
    link = NodeLink(guards, config)
    harness = Harness(link)
    clients = ClientLink(harness, guards)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.path.parent.mkdir(parents=True, exist_ok=True)
        lock = DataDirectoryLock(database.path.parent / "core.lock")
        scheduler = None
        try:
            await database.initialize()
            await harness.executions.resume_verification()
            scheduler = asyncio.create_task(harness.run())
            yield
        finally:
            try:
                if scheduler is not None:
                    scheduler.cancel()
                    await asyncio.gather(scheduler, return_exceptions=True)
                await link.close()
                await clients.close()
            finally:
                lock.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(guards.router)
    app.include_router(link.router)
    app.include_router(HarnessHTTP(harness, guards).router)
    app.include_router(clients.router)
    app.include_router(RecordsHTTP(guards).router)
    app.state.node_link = link
    app.state.harness = harness
    app.state.client_link = clients
    app.state.model_provider = DisabledProvider()
    app.state.decision_model = DisabledDecisionModel()
    web = files("nexus.web")
    app.mount("/static", StaticFiles(directory=str(web)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(web.joinpath("index.html")))

    @app.middleware("http")
    async def response_policy(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'"
        return response

    @app.exception_handler(AccessDenied)
    async def access_error(request: Request, exc: AccessDenied):
        return JSONResponse({"detail": exc.code}, status_code=401 if exc.code == "AUTH_REQUIRED" else 503 if exc.code == "BUSY" else 403, headers={"Cache-Control": "no-store"})

    @app.exception_handler(Conflict)
    async def conflict_error(request: Request, exc: Conflict):
        return JSONResponse({"detail": "BUSY" if str(exc) == "BUSY" else "IDEMPOTENCY_CONFLICT"}, status_code=429 if str(exc) == "BUSY" else 409)

    @app.exception_handler(LookupError)
    async def missing_error(request: Request, exc: LookupError):
        return JSONResponse({"detail": "NOT_FOUND"}, status_code=404)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, exc: sqlite3.Error):
        harness.failed = True
        harness.wake.set()
        await link.close()
        return JSONResponse({"detail": "STORAGE_UNAVAILABLE"}, status_code=503)
    return app
