"""Finite goal and scheduled-work continuation through the existing chat Harness."""

import json
from datetime import timedelta
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
    if require_active:
        current=await one(conn,'SELECT state,last_turn_id FROM plugin_authorities WHERE authority_id=?',(authority['authority_id'],))
        if current is None or current['state']!='active': raise AccessDenied('PLUGIN_AUTONOMY_PAUSED')
        if current['last_turn_id']!=authority['last_turn_id']: raise Conflict('PLUGIN_AUTHORITY_CHANGED')


async def previous_round(conn,authority):
    """A user-paused round may precede new work only after its effects are settled."""
    last=await one(conn,'SELECT * FROM chat_turns WHERE turn_id=?',(authority['last_turn_id'],))
    if last['status']=='completed' and not last['error_code']: return last
    if last['status']!='stopped' or last['error_code']!='PLUGIN_AUTONOMY_PAUSED':
        raise AccessDenied('PLUGIN_PREVIOUS_ROUND_FAILED')
    if last['recovery_required'] or last['work_status'] or last['auto_result_status']=='waiting': raise Conflict('BUSY')
    tasks=await conn.execute_fetchall("""SELECT t.* FROM tasks t WHERE t.task_id IN (
        SELECT k.task_id FROM chat_work_tasks k JOIN chat_work_steps s USING(step_id) WHERE s.turn_id=?
        UNION SELECT k.task_id FROM skill_run_steps k JOIN chat_work_steps s ON s.skill_run_id=k.run_id WHERE s.turn_id=?
    )""",(last['turn_id'],last['turn_id']))
    for task in tasks:
        if task['execution_outcome']=='unknown': raise AccessDenied('EXECUTION_UNKNOWN')
        if task['status'] not in {'succeeded','failed','cancelled','timed_out'} or await one(conn,'SELECT 1 FROM task_attempts WHERE task_id=? AND execution_stopped=0',(task['task_id'],)):
            raise Conflict('BUSY')
    if await one(conn,"SELECT 1 FROM skill_runs r JOIN chat_work_steps s ON s.skill_run_id=r.run_id WHERE s.turn_id=? AND (r.status IN ('running','cancelling') OR EXISTS(SELECT 1 FROM skill_run_steps k WHERE k.run_id=r.run_id AND k.status='unknown'))",(last['turn_id'],)):
        raise AccessDenied('EXECUTION_UNKNOWN')
    if await one(conn,"SELECT 1 FROM extension_tool_runs r JOIN chat_work_steps s ON r.work_step_id=s.step_id OR r.skill_run_id=s.skill_run_id LEFT JOIN sandbox_executions e ON e.execution_id=r.tool_run_id WHERE s.turn_id=? AND (r.status IN ('dispatching','unknown') OR e.status IN ('prepared','sent','unknown'))",(last['turn_id'],)):
        raise AccessDenied('EXECUTION_UNKNOWN')
    return last


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
    async with service.database.transaction() as conn:
        if authority['archived']: raise AccessDenied('CONVERSATION_ARCHIVED')
        await allowed(conn,authority,chat)
        identity=await one(conn,"SELECT token_hash FROM identities WHERE identity_id=? AND role='owner' AND revoked_at IS NULL",(authority['owner_id'],))
        if identity is None: raise AccessDenied('AUTH_REQUIRED')
        if await one(conn,f'SELECT 1 FROM chat_turns WHERE conversation_id=? AND {ACTIVE} LIMIT 1',(authority['conversation_id'],)): return
        last=await previous_round(conn,authority)
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
            await conn.execute("UPDATE plugin_schedule_events SET status='missed' WHERE schedule_id=? AND status='due' AND due_at<? AND NOT EXISTS(SELECT 1 FROM plugin_wakeups w WHERE w.wakeup_id=plugin_schedule_events.event_id)",
                (schedule['schedule_id'],timestamp(now-timedelta(seconds=300))))
            event=await one(conn,"SELECT * FROM plugin_schedule_events e WHERE e.schedule_id=? AND e.status='due' AND NOT EXISTS(SELECT 1 FROM plugin_wakeups w WHERE w.wakeup_id=e.event_id) ORDER BY e.rowid LIMIT 1",(schedule['schedule_id'],))
            if event is None: return
            wakeup=event['event_id'];text='执行已授权定时任务：'+schedule['prompt']
        if last['status']=='stopped':
            text='上轮 '+last['turn_id']+' 已被用户暂停，现已明确恢复。先用 session_task_trace 核对已有执行证据，避免重复操作。'+text
        if await one(conn,'SELECT 1 FROM plugin_wakeups WHERE wakeup_id=?',(wakeup,)): return
        principal=Principal(UUID(authority['owner_id']),'owner',identity[0])
        directory=await work_catalog.catalog(chat,conn,principal,UUID(authority['node_id']))
        if digest_value(directory['catalog'])!=authority['catalog_digest']: raise AccessDenied('PLUGIN_CATALOG_CHANGED')
        await budget(conn,chat,authority['last_turn_id'],chat.config.model_reservation())
    grant=await chat.work.grant(principal,authority['conversation_id'],WorkGrantCreate(request_id=uuid5(NAMESPACE_URL,wakeup+':grant'),node_id=UUID(authority['node_id']),consent=True,profile=authority['profile']))
    async with service.database.transaction() as conn:
        await allowed(conn,authority,chat)
        if authority['kind']=='schedule' and event['due_at']<timestamp(utc_now()-timedelta(seconds=300)):
            await conn.execute("UPDATE plugin_schedule_events SET status='missed' WHERE event_id=? AND NOT EXISTS(SELECT 1 FROM plugin_wakeups WHERE wakeup_id=?)",(wakeup,wakeup))
            return
        conversation=await chat.conversation(conn,principal,authority['conversation_id'])
        request=ChatSubmission(request_id=uuid5(NAMESPACE_URL,wakeup),expected_revision=conversation['revision'],text=text[:2000],allow_model=True,
            allow_tools=True,node_id=UUID(authority['node_id']),work_grant_id=UUID(grant['grant_id']),work_digest=directory['work_digest'],allow_work_results=True)
        await conn.execute("INSERT INTO plugin_wakeups VALUES(?,?,?,NULL,'prepared',?)",(wakeup,authority['authority_id'],canonical(request.model_dump(mode='json')),timestamp(now)))
    result=await chat.submit(principal,authority['conversation_id'],request)
    async with service.database.transaction() as conn:
        await conn.execute("UPDATE plugin_wakeups SET turn_id=?,status='submitted' WHERE wakeup_id=?",(result['turn_id'],wakeup))
        await conn.execute('UPDATE plugin_authorities SET last_turn_id=? WHERE authority_id=?',(result['turn_id'],authority['authority_id']))
        if authority['kind']=='goal': await conn.execute('UPDATE plugin_goals SET rounds=rounds+1 WHERE goal_id=?',(authority['authority_id'],))
