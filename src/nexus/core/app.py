"""S3 application: identity and Node transport, without task dispatch."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from nexus.core.config import CoreConfig
from nexus.core.link.nodes import NodeLink
from nexus.core.security.http import SecurityHTTP, TransportPolicy
from nexus.core.security.service import SecurityService
from nexus.core.storage.database import Database
from nexus.locking import DataDirectoryLock


def create_app(config: CoreConfig) -> FastAPI:
    config = CoreConfig.model_validate_json(config.model_dump_json())
    database = Database(config.data_dir.resolve() / "core.sqlite3")
    guards = SecurityHTTP(SecurityService(database), TransportPolicy(config.origins, config.local_http))
    link = NodeLink(guards, config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.path.parent.mkdir(parents=True, exist_ok=True)
        lock = DataDirectoryLock(database.path.parent / "core.lock")
        try:
            await database.initialize()
            yield
        finally:
            try:
                await link.close()
            finally:
                lock.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(guards.router)
    app.include_router(link.router)
    app.state.node_link = link
    return app
