import asyncio
import secrets
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from nexus.core.security.service import SecurityService
from nexus.core.security.types import NodeScope, Principal
from nexus.core.storage.database import Database
from nexus.core.storage.tasks import TaskRepository


@dataclass
class World:
    db: Database
    security: SecurityService
    tasks: TaskRepository
    owner_id: UUID
    node_id: UUID
    owner_token: SecretStr
    node_token: SecretStr
    owner: Principal
    node: Principal
    scope: NodeScope


@pytest.fixture
def world(tmp_path):
    async def seed():
        db = Database(tmp_path / "core.sqlite3")
        await db.initialize()
        security = SecurityService(db)
        owner_id, node_id = uuid4(), uuid4()
        owner_token, node_token = SecretStr(secrets.token_urlsafe(32)), SecretStr(secrets.token_urlsafe(32))
        await security.register_identity(owner_id, "owner", owner_token)
        await security.register_identity(node_id, "node", node_token)
        owner = await security.authenticate_token(owner_token, "owner")
        node = await security.authenticate_token(node_token, "node")
        tools = frozenset({"system.info@1", "filesystem.list@1"})
        roots = frozenset({"workspace"})
        scope = NodeScope(node_id, tools, roots, tools, roots)
        return World(db, security, TaskRepository(db), owner_id, node_id, owner_token, node_token, owner, node, scope)
    return asyncio.run(seed())
