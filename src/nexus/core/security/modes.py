"""Owner-controlled defaults and conversation overrides; never a model tool."""

import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field, StrictInt

from nexus.contracts.base import StrictContract
from nexus.contracts.extensions import canonical, digest_value
from nexus.core.harness.http import body
from nexus.core.security.service import assert_owner
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict


class ModeChange(StrictContract):
    request_id: UUID
    expected_revision: StrictInt = Field(ge=1)
    mode: Literal['normal', 'full'] | None


async def effective(conn, conversation_id) -> dict:
    global_row = await one(conn, 'SELECT * FROM permission_defaults WHERE singleton=1')
    row = await one(conn, 'SELECT permission_override,permission_revision FROM conversations WHERE conversation_id=?', (str(conversation_id),))
    if row is None:
        raise LookupError('conversation not found')
    inherited = row['permission_override'] is None
    value = {'mode': row['permission_override'], 'effective_mode': global_row['mode'] if inherited else row['permission_override'],
             'source': 'global' if inherited else 'conversation', 'revision': row['permission_revision'],
             'global_mode': global_row['mode'], 'global_revision': global_row['revision']}
    value['fingerprint'] = digest_value({'mode': value['effective_mode'], 'revision': value['revision'],
                                         'global_revision': value['global_revision'] if inherited else None})
    return value


async def bind_grant(conn, grant_id, conversation_id):
    value = await effective(conn, conversation_id)
    await conn.execute('UPDATE chat_work_grants SET permission_mode=?,permission_fingerprint=? WHERE grant_id=?',
                       (value['effective_mode'], value['fingerprint'], str(grant_id)))


async def check_grant(conn, grant):
    value = await effective(conn, grant['conversation_id'])
    if grant['permission_fingerprint']:
        if grant['permission_fingerprint'] != value['fingerprint']:
            raise AccessDenied('PERMISSION_MODE_CHANGED')
    elif value['effective_mode'] != 'normal':
        # A legacy grant must never silently inherit newly enabled full access.
        raise AccessDenied('PERMISSION_MODE_CHANGED')
    return value


class PermissionHTTP:
    def __init__(self, chat, guards):
        self.chat, self.guards = chat, guards
        self.router = APIRouter()
        self.router.add_api_route('/api/v1/permissions/default', self.get_default, methods=['GET'])
        self.router.add_api_route('/api/v1/permissions/default', self.set_default, methods=['PUT'])
        path = '/api/v1/chat/conversations/{conversation_id}/permission'
        self.router.add_api_route(path, self.get_conversation, methods=['GET'])
        self.router.add_api_route(path, self.set_conversation, methods=['PUT'])

    async def get_default(self, request: Request):
        principal = await self.guards.owner_http(request)
        async with self.chat.database.transaction(write=False) as conn:
            await assert_owner(conn, principal, utc_now())
            return dict(await one(conn, 'SELECT mode,revision FROM permission_defaults WHERE singleton=1'))

    async def get_conversation(self, conversation_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        async with self.chat.database.transaction(write=False) as conn:
            await self.chat.conversation(conn, principal, conversation_id)
            return await effective(conn, conversation_id)

    async def change(self, principal, value, conversation_id=None):
        target = str(conversation_id) if conversation_id else 'global'
        encoded = canonical(value.model_dump(mode='json'))
        async with self.chat.harness.operation, self.chat.database.transaction() as conn:
            now = utc_now()
            await assert_owner(conn, principal, now)
            if conversation_id:
                row = await self.chat.conversation(conn, principal, conversation_id)
            elif value.mode is None:
                raise ValueError('global permission mode cannot inherit')
            old = await one(conn, 'SELECT * FROM permission_changes WHERE request_id=?', (str(value.request_id),))
            if old:
                if old['owner_id'] != str(principal.identity_id) or old['target'] != target or old['request_json'] != encoded:
                    raise Conflict('IDEMPOTENCY_CONFLICT')
                return json.loads(old['result_json'])
            if (await one(conn, 'SELECT count(*) FROM permission_changes'))[0] >= 10000:
                raise AccessDenied('PERMISSION_STORAGE_FULL')
            if conversation_id:
                if row['permission_revision'] != value.expected_revision:
                    raise Conflict('IDEMPOTENCY_CONFLICT')
                await conn.execute('UPDATE conversations SET permission_override=?,permission_revision=permission_revision+1,revision=revision+1 WHERE conversation_id=?', (value.mode, target))
                result = await effective(conn, conversation_id)
            else:
                old_default = await one(conn, 'SELECT revision FROM permission_defaults WHERE singleton=1')
                if old_default[0] != value.expected_revision:
                    raise Conflict('IDEMPOTENCY_CONFLICT')
                await conn.execute('UPDATE permission_defaults SET mode=?,revision=revision+1 WHERE singleton=1', (value.mode,))
                result = dict(await one(conn, 'SELECT mode,revision FROM permission_defaults WHERE singleton=1'))
            await conn.execute('INSERT INTO permission_changes VALUES(?,?,?,?,?,?)', (str(value.request_id), str(principal.identity_id), target, encoded, canonical(result), timestamp(now)))
            await audit(conn, str(principal.identity_id), None, 'permission.mode_changed', now)
            return result

    async def set_default(self, request: Request):
        from fastapi import HTTPException
        principal=await self.guards.owner_http(request)
        value=await body(request,ModeChange)
        if value.mode is None: raise HTTPException(400,detail='INVALID_PERMISSION_MODE')
        return await self.change(principal,value)

    async def set_conversation(self, conversation_id: UUID, request: Request):
        return await self.change(await self.guards.owner_http(request), await body(request, ModeChange), conversation_id)
