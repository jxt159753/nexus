"""Explicitly scoped, deterministic sensors using the existing task executor."""

import json
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import Field, StrictInt, StrictStr, model_validator
from typing import Literal

from nexus.contracts.base import StrictContract
from nexus.contracts.basic import relative_path
from nexus.contracts.extensions import canonical, digest_value
from nexus.core.security.modes import effective
from nexus.core.security.service import scope_digest
from nexus.core.security.types import AccessDenied, Principal
from nexus.core.storage.database import one, timestamp, utc_now, audit
from nexus.core.storage.tasks import Conflict, REQUEST


class WatchCreate(StrictContract):
    request_id: UUID
    conversation_id: UUID
    kind: Literal['file', 'device', 'task', 'external']
    node_id: UUID | None = None
    target: StrictStr = Field(min_length=1, max_length=240)
    goal_id: UUID | None = None
    priority: StrictInt = Field(default=0, ge=0, le=9)
    interval_seconds: StrictInt = Field(default=60, ge=30, le=86400)
    max_polls: StrictInt = Field(default=100, ge=1, le=1000)
    duration_minutes: StrictInt = Field(default=60, ge=1, le=10080)

    @model_validator(mode='after')
    def target_scope(self):
        if self.kind in {'file', 'device'} and self.node_id is None:
            raise ValueError('node required')
        if self.kind == 'file':
            relative_path(self.target)
        if self.kind == 'task':
            UUID(self.target)
        return self


class ExternalEvent(StrictContract):
    source_key: StrictStr = Field(min_length=1, max_length=128)
    value: StrictStr = Field(max_length=1000)


async def create(service, conn, principal, value: WatchCreate):
    service.require_enabled()
    conversation = await service.chat.conversation(conn, principal, value.conversation_id)
    if conversation['archived']:
        raise AccessDenied('CONVERSATION_ARCHIVED')
    old = await one(conn, 'SELECT * FROM autonomy_watches WHERE owner_id=? AND request_id=?', (str(principal.identity_id), str(value.request_id)))
    if old:
        if old['request_json'] != value.model_dump_json():
            raise Conflict('IDEMPOTENCY_CONFLICT')
        return dict(old)
    if (await one(conn, 'SELECT count(*) FROM autonomy_watches WHERE paused=0 AND expires_at>?', (timestamp(utc_now()),)))[0] >= 100 or (await one(conn, 'SELECT count(*) FROM autonomy_watches'))[0] >= 1000:
        raise AccessDenied('WATCH_CAPACITY')
    scope = scope_digest(service.chat.harness.scope(value.node_id)) if value.node_id else None
    if value.kind == 'task':
        if not await one(conn, 'SELECT 1 FROM tasks WHERE task_id=? AND owner_id=? AND project_id IS ?', (value.target, str(principal.identity_id), conversation['project_id'])):
            raise AccessDenied('WATCH_SCOPE_DENIED')
    if value.goal_id:
        from nexus.core.plugins.goals import owned
        goal = await owned(conn, principal.identity_id, value.conversation_id, value.goal_id)
        if goal['status'] != 'active':
            raise AccessDenied('GOAL_NOT_ACTIVE')
        if (await one(conn,'SELECT count(*) FROM autonomy_watches WHERE goal_id=?',(str(value.goal_id),)))[0]>=16:
            raise AccessDenied('GOAL_WATCH_LIMIT')
    mode = await effective(conn, value.conversation_id)
    cursor = (await one(conn, "SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
    identifier, now = str(uuid4()), utc_now()
    await conn.execute('''INSERT INTO autonomy_watches(watch_id,owner_id,conversation_id,request_id,request_json,kind,node_id,target,goal_id,priority,interval_seconds,max_polls,next_at,expires_at,scope_digest,permission_fingerprint,cancel_cursor)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (identifier, str(principal.identity_id), str(value.conversation_id), str(value.request_id), value.model_dump_json(), value.kind, str(value.node_id) if value.node_id else None,
        value.target, str(value.goal_id) if value.goal_id else None, value.priority, value.interval_seconds, value.max_polls, timestamp(now), timestamp(now+timedelta(minutes=value.duration_minutes)), scope, mode['fingerprint'], cursor))
    await audit(conn, str(principal.identity_id), None, 'watch.created', now)
    return dict(await one(conn, 'SELECT * FROM autonomy_watches WHERE watch_id=?', (identifier,)))


async def allowed(service, conn, watch):
    now = timestamp(utc_now())
    current = await one(conn, 'SELECT * FROM autonomy_watches WHERE watch_id=?', (watch['watch_id'],))
    if not current or current['paused'] or current['expires_at'] <= now:
        raise AccessDenied('WATCH_ENDED')
    if (await one(conn, 'SELECT paused FROM system_state WHERE singleton=1'))[0]:
        raise AccessDenied('PAUSED')
    owner = await one(conn, "SELECT token_hash FROM identities WHERE identity_id=? AND role='owner' AND revoked_at IS NULL", (watch['owner_id'],))
    conversation = await one(conn, 'SELECT archived,plan_mode FROM conversations WHERE conversation_id=?', (watch['conversation_id'],))
    cursor = (await one(conn, "SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
    mode = await effective(conn, watch['conversation_id'])
    if not owner or conversation['archived'] or conversation['plan_mode'] or cursor != watch['cancel_cursor'] or mode['fingerprint'] != watch['permission_fingerprint']:
        raise AccessDenied('WATCH_AUTHORITY_CHANGED')
    if watch['node_id']:
        if not await one(conn, "SELECT 1 FROM identities WHERE identity_id=? AND role='node' AND revoked_at IS NULL", (watch['node_id'],)):
            raise AccessDenied('WATCH_NODE_REVOKED')
        if scope_digest(service.chat.harness.scope(UUID(watch['node_id']))) != watch['scope_digest']:
            raise AccessDenied('WATCH_SCOPE_CHANGED')
    return Principal(UUID(watch['owner_id']), 'owner', owner[0])


async def emit(conn, watch, key, value):
    old = await one(conn, 'SELECT value_json FROM autonomy_events WHERE watch_id=? AND source_key=?', (watch['watch_id'], key))
    if old:
        if old[0] != canonical(value):
            raise Conflict('IDEMPOTENCY_CONFLICT')
        return
    if (await one(conn, 'SELECT count(*) FROM autonomy_events'))[0] >= 10000:
        raise AccessDenied('EVENT_STORAGE_FULL')
    await conn.execute('INSERT INTO autonomy_events(watch_id,source_key,value_json,created_at) VALUES(?,?,?,?)',
                       (watch['watch_id'], key, canonical(value), timestamp(utc_now())))
    # Wake only an already active goal. A sensor can never resume a user pause,
    # create a goal, grant model permission or reset consumed root limits.
    if watch['goal_id']:
        await conn.execute("UPDATE goal_plans SET last_reason=? WHERE goal_id=?", ('environment_changed', watch['goal_id']))


async def external(service, conn, watch, value: ExternalEvent):
    await allowed(service, conn, watch)
    if watch['kind'] != 'external':
        raise AccessDenied('WATCH_SOURCE_MISMATCH')
    existing=await one(conn,'SELECT 1 FROM autonomy_events WHERE watch_id=? AND source_key=?',(watch['watch_id'],value.source_key))
    current=await one(conn,'SELECT polls,max_polls FROM autonomy_watches WHERE watch_id=?',(watch['watch_id'],))
    if not existing and current['polls']>=current['max_polls']:
        raise AccessDenied('WATCH_EVENT_LIMIT')
    await emit(conn, watch, value.source_key, {'kind':'external', 'value':value.value, 'verification':'owner_supplied_observation'})
    if not existing:
        await conn.execute('UPDATE autonomy_watches SET polls=polls+1 WHERE watch_id=?',(watch['watch_id'],))


async def poll_one(service, watch):
    async with service.chat.harness.operation, service.database.transaction() as conn:
        principal = await allowed(service, conn, watch)
        if watch['polls'] >= watch['max_polls'] and not watch['pending_task_id']:
            await conn.execute('UPDATE autonomy_watches SET paused=1 WHERE watch_id=?', (watch['watch_id'],))
            return
        value = None
        if watch['kind'] == 'file':
            if watch['pending_task_id']:
                task = await one(conn, 'SELECT * FROM tasks WHERE task_id=?', (watch['pending_task_id'],))
                if task['status'] not in {'succeeded','failed','cancelled','timed_out'}:
                    return
                if task['execution_outcome'] == 'unknown' or await one(conn, 'SELECT 1 FROM task_attempts WHERE task_id=? AND execution_stopped=0', (task['task_id'],)):
                    raise AccessDenied('EXECUTION_UNKNOWN')
                if task['status'] != 'succeeded':
                    await emit(conn, watch, task['task_id'], {'kind':'sensor_failed','task_id':task['task_id'],'status':task['status'],
                        'message':'文件观察未完成，已暂停订阅；请查看关联任务的执行结果，核对后重新配置。'})
                    await conn.execute('UPDATE autonomy_watches SET paused=1 WHERE watch_id=?', (watch['watch_id'],))
                    return
                result = json.loads(task['result_json'])
                value = {'kind':'file','path':watch['target'],'sha256':result['sha256']}
                await conn.execute('UPDATE autonomy_watches SET pending_task_id=NULL WHERE watch_id=?', (watch['watch_id'],))
            else:
                # Persist the stable request before calling the Harness outside
                # this transaction. Retries after a crash resolve the same task.
                return ('read', principal)
        elif watch['kind'] == 'device':
            connection = service.chat.harness.link.connections.get(UUID(watch['node_id']))
            value = {'kind':'device','node_id':watch['node_id'],'online':bool(connection and connection.ready)}
        elif watch['kind'] == 'task':
            task = await one(conn, 'SELECT status,execution_outcome,revision FROM tasks WHERE task_id=? AND owner_id=?', (watch['target'], watch['owner_id']))
            if task is None:
                raise AccessDenied('WATCH_SCOPE_DENIED')
            value = {'kind':'task','task_id':watch['target'],**dict(task)}
        if value is None:
            return
        digest = digest_value(value)
        if watch['last_digest'] is not None and watch['last_digest'] != digest:
            await emit(conn, watch, str(watch['polls'])+':'+digest, value)
        await conn.execute('UPDATE autonomy_watches SET last_digest=?,polls=polls+1,next_at=? WHERE watch_id=?',
                           (digest, timestamp(utc_now()+timedelta(seconds=watch['interval_seconds'])), watch['watch_id']))


async def tick(service):
    if not service.config.enabled:
        return
    async with service.database.transaction(write=False) as conn:
        if (await one(conn, 'SELECT paused FROM system_state WHERE singleton=1'))[0]:
            return
        watches = await conn.execute_fetchall("SELECT * FROM autonomy_watches WHERE paused=0 AND kind!='external' AND next_at<=? ORDER BY priority DESC,next_at LIMIT 10", (timestamp(utc_now()),))
    for watch in watches:
        try:
            result = await poll_one(service, watch)
            if result:
                _, principal = result
                request = REQUEST.validate_python({'request_id':uuid5(NAMESPACE_URL, f"watch:{watch['watch_id']}:{watch['polls']}"), 'intent':'read_file', 'arguments':{'root_id':'workspace','path':watch['target']}})
                async with service.chat.harness.operation:
                    async with service.database.transaction(write=False) as conn:
                        await allowed(service, conn, watch)
                    task = await service.chat.harness.submit(principal, UUID(watch['node_id']), request, rate_limit=False)
                    async with service.database.transaction() as conn:
                        await conn.execute('UPDATE autonomy_watches SET pending_task_id=? WHERE watch_id=?', (str(task.task_id), watch['watch_id']))
        except (AccessDenied, Conflict, ValueError, LookupError) as exc:
            async with service.chat.harness.operation,service.database.transaction() as conn:
                await conn.execute('UPDATE autonomy_watches SET paused=1 WHERE watch_id=?', (watch['watch_id'],))
                if watch['pending_task_id']:
                    from nexus.core.storage.control import cancel_task
                    from nexus.core.storage.tasks import load_task
                    task=await load_task(conn,UUID(watch['pending_task_id']))
                    if task.status not in {'succeeded','failed','cancelled','timed_out'}:
                        await cancel_task(conn,task,UUID(watch['owner_id']),utc_now())
                from nexus.core.plugins.health import alert
                await alert(conn,'sensors','warning','部分观察任务已暂停；请查看订阅状态和执行记录。')
                if (await one(conn,'SELECT count(*) FROM autonomy_events'))[0]<10000:
                    await emit(conn, watch, 'blocked:'+str(watch['polls']), {'kind':'sensor_blocked','reason':getattr(exc, 'code', str(exc))[:200],
                        'message':'观察条件已失效或执行尚未核实，已暂停订阅；已有证据保留，请核对授权、期限及关联任务。'})
