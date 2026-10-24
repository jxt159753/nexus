"""Finite goal and scheduled-work continuation through the existing chat Harness."""

import json
from uuid import UUID,uuid5,NAMESPACE_URL

from nexus.contracts.chat import ChatSubmission
from nexus.contracts.work import WorkGrantCreate
from nexus.contracts.extensions import canonical,digest_value
from nexus.core.cognitive.chat import ACTIVE
from nexus.core.cognitive import work_catalog,chat_compaction
from nexus.core.security.modes import effective
from nexus.core.security.service import scope_digest
from nexus.core.security.types import Principal,AccessDenied
from nexus.core.storage.database import one,utc_now,timestamp
from nexus.core.storage.tasks import Conflict


async def attach(service,conn,identifier,kind,conversation,step,grant):
    turn=await one(conn,'SELECT * FROM chat_turns WHERE turn_id=?',(step['turn_id'],))
    mode=await effective(conn,conversation['conversation_id'])
    cursor=(await one(conn,"SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
    await conn.execute('INSERT INTO plugin_authorities VALUES(?,?,?,?,?,?,?,?,?,?,?,\'active\') ON CONFLICT(authority_id) DO UPDATE SET root_turn_id=excluded.root_turn_id,last_turn_id=excluded.last_turn_id,node_id=excluded.node_id,profile=excluded.profile,scope_digest=excluded.scope_digest,catalog_digest=excluded.catalog_digest,permission_fingerprint=excluded.permission_fingerprint,cancel_cursor=excluded.cancel_cursor,state=\'active\'',
        (identifier,kind,conversation['conversation_id'],step['turn_id'],step['turn_id'],grant['node_id'],grant['profile'],grant['scope_digest'],
         digest_value(json.loads(turn['work_catalog_json'])),mode['fingerprint'],cursor))


async def allowed(conn,authority,chat,*,require_active=True):
    mode=await effective(conn,authority['conversation_id'])
    cursor=(await one(conn,"SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
    if mode['fingerprint']!=authority['permission_fingerprint'] or cursor!=authority['cancel_cursor']:
        raise AccessDenied('PLUGIN_AUTONOMY_AUTHORITY_CHANGED')
    if scope_digest(chat.harness.scope(UUID(authority['node_id'])))!=authority['scope_digest']:
        raise AccessDenied('PLUGIN_AUTONOMY_SCOPE_CHANGED')
    if require_active and authority['state']!='active': raise AccessDenied('PLUGIN_AUTONOMY_PAUSED')


async def budget(conn,chat,turn_id,reservation):
    authority=await one(conn,'SELECT a.* FROM plugin_authorities a LEFT JOIN plugin_wakeups w USING(authority_id) WHERE a.root_turn_id=? OR w.turn_id=? LIMIT 1',(turn_id,turn_id))
    if authority is None: return reservation
    await allowed(conn,authority,chat,require_active=False)
    # All continuation turns share the original USD 0.05 root allowance.
    calls=await conn.execute_fetchall('SELECT DISTINCT m.* FROM model_calls m LEFT JOIN chat_compactions p USING(call_id) WHERE coalesce(m.chat_turn_id,m.work_root_turn_id,p.turn_id) IN (SELECT root_turn_id FROM plugin_authorities WHERE root_turn_id=? UNION SELECT w.turn_id FROM plugin_wakeups w JOIN plugin_authorities a USING(authority_id) WHERE a.root_turn_id=?)',
        (authority['root_turn_id'],authority['root_turn_id']))
    return min(reservation,chat_compaction.remaining(chat.config,calls))


async def recover(service):
    async with service.database.transaction() as conn:
        await conn.execute("UPDATE plugin_authorities SET state='interrupted' WHERE state='active'")
        await conn.execute("UPDATE plugin_wakeups SET status='interrupted' WHERE status='prepared'")
        await conn.execute("UPDATE plugin_goals SET status='paused' WHERE status='active'")


async def tick(service):
    if not service.config.enabled: return
    chat=service.chat
    async with service.database.transaction(write=False) as conn:
        if (await one(conn,'SELECT paused FROM system_state WHERE singleton=1'))[0]: return
        authorities=await conn.execute_fetchall("SELECT a.*,c.owner_id,c.archived,c.revision FROM plugin_authorities a JOIN conversations c USING(conversation_id) WHERE a.state='active' LIMIT 100")
    for authority in authorities:
        try:
            await advance(service,authority)
        except (AccessDenied,ValueError,LookupError,Conflict):
            async with service.database.transaction() as conn:
                await conn.execute("UPDATE plugin_authorities SET state='paused' WHERE authority_id=?",(authority['authority_id'],))
                if authority['kind']=='goal': await conn.execute("UPDATE plugin_goals SET status='paused' WHERE goal_id=? AND status='active'",(authority['authority_id'],))


async def advance(service,authority):
    chat=service.chat;now=utc_now()
    async with service.database.transaction(write=False) as conn:
        if authority['archived']: raise AccessDenied('CONVERSATION_ARCHIVED')
        await allowed(conn,authority,chat)
        identity=await one(conn,"SELECT token_hash FROM identities WHERE identity_id=? AND role='owner' AND revoked_at IS NULL",(authority['owner_id'],))
        if identity is None: raise AccessDenied('AUTH_REQUIRED')
        if await one(conn,f'SELECT 1 FROM chat_turns WHERE conversation_id=? AND {ACTIVE} LIMIT 1',(authority['conversation_id'],)): return
        last=await one(conn,'SELECT * FROM chat_turns WHERE turn_id=?',(authority['last_turn_id'],))
        if last['status']!='completed' or last['error_code']: raise AccessDenied('PLUGIN_PREVIOUS_ROUND_FAILED')
        if authority['kind']=='goal':
            goal=await one(conn,'SELECT * FROM plugin_goals WHERE goal_id=?',(authority['authority_id'],))
            if goal['status']!='active': return
            if goal['rounds']>=goal['max_rounds'] or goal['expires_at']<=timestamp(now): raise AccessDenied('PLUGIN_GOAL_LIMIT')
            latest=await one(conn,'SELECT turn_id FROM chat_turns WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1',(authority['conversation_id'],))
            if latest[0]!=authority['last_turn_id']: raise AccessDenied('PLUGIN_GOAL_USER_ACTIVITY')
            wakeup=f"goal:{goal['goal_id']}:{goal['rounds']+1}"
            text='继续已授权目标：'+goal['objective']+'。检查已有证据后推进；完成时 update_goal。遇到阻塞如实记录，不能扩大范围。'
        else:
            schedule=await one(conn,'SELECT * FROM plugin_schedules WHERE schedule_id=?',(authority['authority_id'],))
            if schedule['deleted']: return
            event=await one(conn,"SELECT * FROM plugin_schedule_events e WHERE e.schedule_id=? AND e.status='due' AND NOT EXISTS(SELECT 1 FROM plugin_wakeups w WHERE w.wakeup_id=e.event_id) ORDER BY e.rowid LIMIT 1",(schedule['schedule_id'],))
            if event is None: return
            if (now-__import__('datetime').datetime.fromisoformat(event['due_at'])).total_seconds()>300: return
            wakeup=event['event_id'];text='执行已授权定时任务：'+schedule['prompt']
        if await one(conn,'SELECT 1 FROM plugin_wakeups WHERE wakeup_id=?',(wakeup,)): return
        principal=Principal(UUID(authority['owner_id']),'owner',identity[0])
        directory=await work_catalog.catalog(chat,conn,principal,UUID(authority['node_id']))
        if digest_value(directory['catalog'])!=authority['catalog_digest']: raise AccessDenied('PLUGIN_CATALOG_CHANGED')
        await budget(conn,chat,authority['last_turn_id'],chat.config.task_cost_microunits)
    grant=await chat.work.grant(principal,authority['conversation_id'],WorkGrantCreate(request_id=uuid5(NAMESPACE_URL,wakeup+':grant'),node_id=UUID(authority['node_id']),consent=True,profile=authority['profile']))
    async with service.database.transaction() as conn:
        await allowed(conn,authority,chat)
        conversation=await chat.conversation(conn,principal,authority['conversation_id'])
        request=ChatSubmission(request_id=uuid5(NAMESPACE_URL,wakeup),expected_revision=conversation['revision'],text=text[:2000],allow_model=True,
            allow_tools=True,node_id=UUID(authority['node_id']),work_grant_id=UUID(grant['grant_id']),work_digest=directory['work_digest'],allow_work_results=True)
        await conn.execute("INSERT INTO plugin_wakeups VALUES(?,?,?,NULL,'prepared',?)",(wakeup,authority['authority_id'],canonical(request.model_dump(mode='json')),timestamp(now)))
    result=await chat.submit(principal,authority['conversation_id'],request)
    async with service.database.transaction() as conn:
        await conn.execute("UPDATE plugin_wakeups SET turn_id=?,status='submitted' WHERE wakeup_id=?",(result['turn_id'],wakeup))
        await conn.execute('UPDATE plugin_authorities SET last_turn_id=? WHERE authority_id=?',(result['turn_id'],authority['authority_id']))
        if authority['kind']=='goal': await conn.execute('UPDATE plugin_goals SET rounds=rounds+1 WHERE goal_id=?',(authority['authority_id'],))
