import asyncio
import secrets
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from nexus.core.security.service import SecurityService
from nexus.core.security.types import NodeScope, Principal
from nexus.core.storage.database import Database
from nexus.core.storage.tasks import TaskRepository


@pytest.fixture(autouse=True)
def synthetic_model_pricing(monkeypatch):
    """Mock-provider tests use a fresh quote; explicit expiry cases stay explicit."""
    from nexus.core.models.config import ModelConfig
    from nexus.core.storage.database import utc_now
    original = ModelConfig.__init__
    def initialize(self, **values):
        values.setdefault("price_valid_until", utc_now().date() + timedelta(days=1))
        original(self, **values)
    monkeypatch.setattr(ModelConfig, "__init__", initialize)


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
