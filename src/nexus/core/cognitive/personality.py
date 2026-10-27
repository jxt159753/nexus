"""Account-wide assistant style, snapshotted by ChatService for each new turn."""

from fastapi import APIRouter, Request

from nexus.contracts.personality import AssistantStyle, AssistantStyleUpdate
from nexus.core.harness.http import body
from nexus.core.security.service import assert_owner
from nexus.core.models.personality import style_catalog
from nexus.core.storage.database import audit, one, utc_now
from nexus.core.storage.tasks import Conflict


async def preferences(conn, owner_id) -> dict:
    row = await one(conn, 'SELECT personality,emoji,revision FROM assistant_preferences WHERE owner_id=?', (str(owner_id),))
    return dict(row) if row else {**AssistantStyle().model_dump(), 'revision': 0}


class PersonalityHTTP:
    def __init__(self, database, guards):
        self.database, self.guards = database, guards
        self.router = APIRouter()
        self.router.add_api_route('/api/v1/assistant/preferences', self.get, methods=['GET'])
        self.router.add_api_route('/api/v1/assistant/preferences', self.put, methods=['PUT'])

    async def get(self, request: Request):
        principal = await self.guards.owner_http(request)
        async with self.database.transaction(write=False) as conn:
            await assert_owner(conn, principal, utc_now())
            return {'preferences': await preferences(conn, principal.identity_id), **style_catalog()}

    async def change(self, principal, value: AssistantStyleUpdate):
        async with self.database.transaction() as conn:
            now = utc_now()
            await assert_owner(conn, principal, now)
            old = await preferences(conn, principal.identity_id)
            if old['revision'] != value.expected_revision:
                raise Conflict('IDEMPOTENCY_CONFLICT')
            selected = value.model_dump(exclude={'expected_revision'})
            if selected == {key: old[key] for key in selected}:
                return old
            revision = old['revision'] + 1
            await conn.execute('INSERT INTO assistant_preferences VALUES(?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET personality=excluded.personality,emoji=excluded.emoji,revision=excluded.revision',
                               (str(principal.identity_id), value.personality, value.emoji, revision))
            await audit(conn, str(principal.identity_id), None, 'assistant.style_changed', now)
            return {**selected, 'revision': revision}

    async def put(self, request: Request):
        principal = await self.guards.owner_http(request)
        return {'preferences': await self.change(principal, await body(request, AssistantStyleUpdate))}
