"""Durable owner reminders. Delivery credentials can only read their own inbox."""

import hashlib
import json
import secrets
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, StrictStr

from nexus.contracts.base import StrictContract
from nexus.core.harness.http import body
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict


class Register(StrictContract):
    client_id: UUID
    name: StrictStr = Field(min_length=1, max_length=80)
    platform: Literal['android', 'windows', 'web']


class ReminderCreate(StrictContract):
    request_id: UUID
    conversation_id: UUID
    title: StrictStr = Field(min_length=1, max_length=100)
    body: StrictStr = Field(min_length=1, max_length=2000)
    targets: list[UUID] = Field(default_factory=list, max_length=32)


async def publish(conn, owner_id, conversation_id, source_key, title, content, targets=None):
    """Commit the content and each selected destination in the caller's transaction."""
    owner_id, conversation_id = str(owner_id), str(conversation_id)
    target_spec = json.dumps(sorted({str(value) for value in targets}) if targets is not None else None)
    existing = await one(conn, 'SELECT * FROM reminders WHERE owner_id=? AND source_key=?', (owner_id, source_key))
    if existing:
        if (existing['conversation_id'],existing['title'],existing['body'],existing['targets_json']) != (conversation_id,title[:100],content[:2000],target_spec):
            raise Conflict('IDEMPOTENCY_CONFLICT')
        return existing['reminder_id']
    clients = await conn.execute_fetchall('SELECT client_id FROM reminder_clients WHERE owner_id=? AND enabled=1', (owner_id,))
    available = {row[0] for row in clients}
    selected = available if targets is None else {str(value) for value in targets}
    if not selected <= available:
        raise ValueError('提醒目标已停用或不属于当前用户')
    identifier = str(uuid4())
    await conn.execute('INSERT INTO reminders VALUES(?,?,?,?,?,?,?,NULL,?)',
                       (identifier, owner_id, conversation_id, source_key, title[:100], content[:2000], timestamp(utc_now()), target_spec))
    await conn.executemany('INSERT INTO reminder_deliveries(reminder_id,client_id) VALUES(?,?)',
                           [(identifier, target) for target in sorted(selected)])
    return identifier


class NotificationHTTP:
    def __init__(self, service, guards):
        self.service, self.guards = service, guards
        self.router = APIRouter(prefix='/api/v1/reminders')
        for path, method, handler in [
            ('', 'GET', self.list), ('', 'POST', self.create),
            ('/clients', 'GET', self.clients), ('/clients', 'POST', self.register),
            ('/clients/{client_id}', 'DELETE', self.revoke),
            ('/delivery', 'GET', self.delivery),
            ('/delivery/{reminder_id}', 'POST', self.posted),
            ('/{reminder_id}/read', 'POST', self.read),
        ]:
            self.router.add_api_route(path, handler, methods=[method])

    async def clients(self, request: Request):
        principal = await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.owner(conn, principal)
            rows = await conn.execute_fetchall('SELECT client_id,name,platform,enabled,last_seen_at FROM reminder_clients WHERE owner_id=? ORDER BY created_at', (str(principal.identity_id),))
            return {'clients': [dict(row) for row in rows]}

    async def register(self, request: Request):
        principal = await self.guards.owner_http(request)
        value = await body(request, Register)
        token = secrets.token_urlsafe(32)
        async with self.service.database.transaction() as conn:
            await self.service.chat.owner(conn, principal)
            old = await one(conn, 'SELECT owner_id FROM reminder_clients WHERE client_id=?', (str(value.client_id),))
            if old and old[0] != str(principal.identity_id):
                raise AccessDenied()
            if not old and (await one(conn, 'SELECT count(*) FROM reminder_clients WHERE owner_id=?', (str(principal.identity_id),)))[0] >= 32:
                raise HTTPException(409, detail='最多登记32个提醒客户端')
            await conn.execute('''INSERT INTO reminder_clients(client_id,owner_id,token_hash,name,platform,created_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(client_id) DO UPDATE SET token_hash=excluded.token_hash,name=excluded.name,platform=excluded.platform,enabled=1''',
                (str(value.client_id), str(principal.identity_id), hashlib.sha256(token.encode()).hexdigest(), value.name, value.platform, timestamp(utc_now())))
            await audit(conn, str(principal.identity_id), None, 'reminder.client_enabled', utc_now())
        return {'client_id': str(value.client_id), 'token': token}

    async def revoke(self, client_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        async with self.service.database.transaction() as conn:
            await self.service.chat.owner(conn, principal)
            row = await conn.execute('UPDATE reminder_clients SET enabled=0 WHERE client_id=? AND owner_id=?', (str(client_id), str(principal.identity_id)))
            if not row.rowcount:
                raise LookupError('client not found')
            await audit(conn, str(principal.identity_id), None, 'reminder.client_disabled', utc_now())
        return {'enabled': False}

    async def authenticate(self, conn, request):
        self.guards._transport(request, origin=False)
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer ') or not 40 <= len(authorization[7:]) <= 128:
            raise AccessDenied('AUTH_REQUIRED')
        row = await one(conn, '''SELECT c.* FROM reminder_clients c JOIN identities i ON i.identity_id=c.owner_id
            WHERE c.token_hash=? AND c.enabled=1 AND i.revoked_at IS NULL''', (hashlib.sha256(authorization[7:].encode()).hexdigest(),))
        if row is None:
            raise AccessDenied('AUTH_REQUIRED')
        return row

    async def delivery(self, request: Request):
        async with self.service.database.transaction() as conn:
            client = await self.authenticate(conn, request)
            await conn.execute('UPDATE reminder_clients SET last_seen_at=? WHERE client_id=?', (timestamp(utc_now()), client['client_id']))
            rows = await conn.execute_fetchall('''SELECT r.* FROM reminders r JOIN reminder_deliveries d USING(reminder_id)
                WHERE d.client_id=? AND d.posted_at IS NULL AND r.read_at IS NULL ORDER BY r.created_at LIMIT 20''', (client['client_id'],))
            return {'reminders': [dict(row) for row in rows]}

    async def posted(self, reminder_id: UUID, request: Request):
        async with self.service.database.transaction() as conn:
            client = await self.authenticate(conn, request)
            changed = await conn.execute('UPDATE reminder_deliveries SET posted_at=coalesce(posted_at,?) WHERE reminder_id=? AND client_id=?',
                                         (timestamp(utc_now()), str(reminder_id), client['client_id']))
            if not changed.rowcount:
                raise LookupError('delivery not found')
        return {'posted': True, 'verification': 'client_reported_notification_posted'}

    async def list(self, request: Request, before: str | None = None):
        principal = await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.owner(conn, principal)
            rows = await conn.execute_fetchall('SELECT * FROM reminders WHERE owner_id=? AND (? IS NULL OR created_at<?) ORDER BY created_at DESC LIMIT 50', (str(principal.identity_id), before, before))
            result = []
            for row in rows:
                deliveries = await conn.execute_fetchall('SELECT d.client_id,c.name,d.posted_at FROM reminder_deliveries d JOIN reminder_clients c USING(client_id) WHERE reminder_id=?', (row['reminder_id'],))
                result.append(dict(row) | {'deliveries': [dict(item) for item in deliveries]})
            return {'reminders': result, 'next_before': rows[-1]['created_at'] if len(rows) == 50 else None}

    async def create(self, request: Request):
        principal = await self.guards.owner_http(request)
        value = await body(request, ReminderCreate, max_bytes=16384)
        async with self.service.database.transaction() as conn:
            await self.service.chat.conversation(conn, principal, value.conversation_id)
            try:
                identifier = await publish(conn, principal.identity_id, value.conversation_id, 'manual:' + str(value.request_id), value.title, value.body, value.targets or None)
            except Conflict:
                raise
            except ValueError as exc:
                raise HTTPException(400, detail=str(exc)) from None
        return {'reminder_id': identifier}

    async def read(self, reminder_id: UUID, request: Request):
        principal = await self.guards.owner_http(request)
        async with self.service.database.transaction() as conn:
            await self.service.chat.owner(conn, principal)
            row = await conn.execute('UPDATE reminders SET read_at=coalesce(read_at,?) WHERE reminder_id=? AND owner_id=?', (timestamp(utc_now()), str(reminder_id), str(principal.identity_id)))
            if not row.rowcount:
                raise LookupError('reminder not found')
        return {'read': True}
