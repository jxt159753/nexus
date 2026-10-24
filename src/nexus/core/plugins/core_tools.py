"""Core-owned plugin operations; no plugin-supplied code enters this module."""

import json
from datetime import timedelta
from uuid import uuid4

from nexus.contracts.extensions import canonical
from nexus.core.plugins.service import public_version
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now, audit


async def owned_conversation(conn,principal,current,identifier):
    row=await one(conn,'SELECT * FROM conversations WHERE conversation_id=? AND owner_id=? AND project_id IS ? AND archived=0',
                  (identifier,str(principal.identity_id),current['project_id']))
    if row is None: raise AccessDenied('SESSION_SCOPE_DENIED')
    return row


async def visible_turn(conn,principal,current,identifier):
    row=await one(conn,'SELECT t.* FROM chat_turns t JOIN conversations c USING(conversation_id) WHERE t.turn_id=? AND t.owner_id=? AND c.project_id IS ? AND c.archived=0',
                  (identifier,str(principal.identity_id),current['project_id']))
    if row is None: raise AccessDenied('SESSION_SCOPE_DENIED')
    # Explicit private attachments, memories and references keep their own scope.
    if row['memory_refs_json']!='[]' or row['references_json']!='[]' or row['attachments_json']!='[]':
        raise AccessDenied('PRIVATE_REFERENCE_PREVIEW_REQUIRED')
    return row


async def execute(service,conn,principal,conversation,step,grant,plugin_id,name,value):
    now=utc_now(); conversation_id=conversation['conversation_id']
    if plugin_id=='builtin.skill':
        from nexus.core.cognitive.chat_skills import eligible
        from uuid import UUID
        rows=await conn.execute_fetchall("SELECT * FROM extension_versions WHERE owner_id=? AND kind='skill' AND enabled=1 ORDER BY rowid DESC LIMIT 32",(str(principal.identity_id),))
        skills=[]
        for row in rows:
            try: await eligible(service.chat.skills,conn,str(principal.identity_id),row,UUID(grant['node_id']))
            except AccessDenied: continue
            manifest=json.loads(row['manifest_json'])
            if value.get('version_id')==row['version_id']:
                return {'version_id':row['version_id'],'artifact_digest':row['artifact_digest'],'description':manifest['description'],'markdown':row['markdown_text'],'steps':json.loads(row['steps_json'])}
            skills.append({'version_id':row['version_id'],'artifact_digest':row['artifact_digest'],'description':manifest['description']})
        if value.get('version_id'): raise AccessDenied('VERSION_NOT_APPROVED')
        return {'skills':skills}
    if plugin_id=='builtin.clock':
        from nexus.core.cognitive.work_queries import query
        return await query(conn,step,grant,name,{'utc_offset_minutes':value.get('utc_offset_minutes',480)},now)
    if plugin_id=='builtin.session':
        if name=='session_search':
            rows=await conn.execute_fetchall("SELECT conversation_id,title FROM conversations WHERE owner_id=? AND project_id IS ? AND archived=0 AND (instr(lower(title),lower(?))>0 OR conversation_id IN (SELECT conversation_id FROM chat_turns WHERE memory_refs_json='[]' AND references_json='[]' AND attachments_json='[]' AND instr(lower(text),lower(?))>0)) ORDER BY rowid DESC LIMIT ?",
                (str(principal.identity_id),conversation['project_id'],value['query'],value['query'],value.get('limit',10)))
            return {'sessions':[dict(row) for row in rows]}
        if name in {'session_event_search','session_trace'}:
            target=await owned_conversation(conn,principal,conversation,value['conversation_id'])
            query=value.get('query','')
            rows=await conn.execute_fetchall("SELECT turn_id,status,text,response_text,memory_refs_json,references_json,attachments_json FROM chat_turns WHERE conversation_id=? AND (instr(lower(text),lower(?))>0 OR instr(lower(response_text),lower(?))>0) ORDER BY rowid DESC LIMIT ?",
                (target['conversation_id'],query,query,value.get('limit',10)))
            return {'turns':[{'turn_id':row['turn_id'],'status':row['status'],'input':row['text'][:500],'response':row['response_text'][:1000]} for row in rows if row['memory_refs_json']=='[]' and row['references_json']=='[]' and row['attachments_json']=='[]']}
        turn=await visible_turn(conn,principal,conversation,value['turn_id'])
        if name=='session_event_read':
            return {'turn_id':turn['turn_id'],'status':turn['status'],'input':turn['text'],'response':turn['response_text'][:6000],'truncated':len(turn['response_text'])>6000}
        rows=await conn.execute_fetchall('SELECT task_id,status,execution_outcome,verification FROM tasks WHERE owner_id=? AND task_id IN (SELECT k.task_id FROM chat_work_tasks k JOIN chat_work_steps w USING(step_id) WHERE w.turn_id=?) LIMIT 20',(str(principal.identity_id),turn['turn_id']))
        calls=await conn.execute_fetchall('SELECT call_id,outcome,cost_status,cost_microunits FROM model_calls WHERE chat_turn_id=? OR work_root_turn_id=? LIMIT 20',(turn['turn_id'],turn['turn_id']))
        return {'turn_id':turn['turn_id'],'tasks':[dict(row) for row in rows],'calls':[dict(row) for row in calls]}
    if plugin_id=='builtin.todo':
        todos=value['todos']
        if len({item['id'] for item in todos})!=len(todos): raise ValueError('todo identifiers must be unique')
        await conn.execute('INSERT INTO plugin_todos VALUES(?,?,1) ON CONFLICT(conversation_id) DO UPDATE SET value_json=excluded.value_json,revision=revision+1',(conversation_id,canonical(todos)))
        return {'todos':todos,'verification':'planning_status_only','revision':(await one(conn,'SELECT revision FROM plugin_todos WHERE conversation_id=?',(conversation_id,)))[0]}
    if plugin_id=='builtin.interaction':
        if name=='exit_plan_mode' and not conversation['plan_mode']: raise ValueError('plan mode is not active')
        if step['permission_text']:
            if name=='exit_plan_mode': await conn.execute('UPDATE conversations SET plan_mode=0,revision=revision+1 WHERE conversation_id=?',(conversation_id,))
            return {'answer':step['permission_text'],'plan_approved':name=='exit_plan_mode'}
        if name=='exit_plan_mode' and grant['permission_mode']=='full':
            await conn.execute('UPDATE conversations SET plan_mode=0,revision=revision+1 WHERE conversation_id=?',(conversation_id,))
            return {'plan':value['plan'],'plan_approved':True}
        question=value.get('question',value.get('plan',''))
        await conn.execute("UPDATE chat_work_steps SET status='permission',permission_decision=NULL,request_text=?,result_json=? WHERE step_id=?",
            (question,canonical({'question':question,'options':value.get('options',[]),'kind':'plan' if name=='exit_plan_mode' else 'question'}),step['step_id']))
        await conn.execute("UPDATE chat_turns SET work_status='permission',revision=revision+1 WHERE turn_id=?",(step['turn_id'],))
        return None
    if plugin_id=='builtin.goal':
        if name=='create_goal':
            old=await one(conn,"SELECT goal_id FROM plugin_goals WHERE conversation_id=? AND status='active' AND expires_at>?",(conversation_id,timestamp(now)))
            if old: raise ValueError('an active goal already exists')
            identifier=str(uuid4())
            await conn.execute("INSERT INTO plugin_goals(goal_id,conversation_id,owner_id,objective,status,max_rounds,created_at,expires_at) VALUES(?,?,?,?,'active',?,?,?)",
                (identifier,conversation_id,str(principal.identity_id),value['objective'],value.get('max_rounds',5),timestamp(now),timestamp(now+timedelta(minutes=value.get('duration_minutes',30)))))
            await conn.execute('UPDATE plugin_goals SET rounds=1 WHERE goal_id=?',(identifier,))
            from nexus.core.plugins.lifecycle import attach
            await attach(service,conn,identifier,'goal',conversation,step,grant)
            return dict(await one(conn,'SELECT * FROM plugin_goals WHERE goal_id=?',(identifier,)))
        if name=='get_goal':
            row=await one(conn,'SELECT * FROM plugin_goals WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1',(conversation_id,))
            return {'goal':dict(row) if row else None}
        row=await one(conn,'SELECT * FROM plugin_goals WHERE goal_id=? AND conversation_id=? AND owner_id=?',(value['goal_id'],conversation_id,str(principal.identity_id)))
        if row is None: raise LookupError('goal not found')
        if value['status']=='blocked' and row['rounds']<3: raise ValueError('blocked requires at least three goal rounds')
        verified=False
        if value['status']=='complete':
            # Per-task evidence remains in traces. The model cannot certify
            # arbitrary business objectives through a status declaration.
            verified=False
        await conn.execute('UPDATE plugin_goals SET status=?,completion_verified=? WHERE goal_id=?',(value['status'],int(verified),row['goal_id']))
        if value['status']=='active':
            from nexus.core.plugins.lifecycle import attach
            await attach(service,conn,row['goal_id'],'goal',conversation,step,grant)
        else:
            await conn.execute("UPDATE plugin_authorities SET state='paused' WHERE authority_id=?",(row['goal_id'],))
        return {'goal_id':row['goal_id'],'status':value['status'],'completion_verified':verified,'verification':'goal_state_recorded'}
    if plugin_id=='builtin.schedule':
        from nexus.core.plugins.schedule import manage
        result=await manage(conn,principal,conversation_id,name,value,now)
        if name=='schedule_create' and value.get('execute') or name=='schedule_update' and value.get('paused') is False:
            from nexus.core.plugins.lifecycle import attach
            existing=await one(conn,'SELECT 1 FROM plugin_authorities WHERE authority_id=?',(result['schedule_id'],))
            if name=='schedule_create' or existing: await attach(service,conn,result['schedule_id'],'schedule',conversation,step,grant)
        return result
    if plugin_id=='builtin.manager':
        if name=='plugin_inspect_list' or name=='plugin_manager' and value['action']=='list':
            return {'plugins':await service.summaries(conn,principal.identity_id)}
        if name=='plugin_inspect_query' or value.get('action')=='inspect':
            row,_=await service.resolve(conn,principal.identity_id,value['plugin_id'])
            return public_version(row,True)
        row=await one(conn,'SELECT * FROM plugin_versions WHERE version_id=? AND owner_id=?',(value.get('version_id',''),str(principal.identity_id)))
        if row is None: raise LookupError('plugin version not found')
        action=value['action']
        return await service.decide(conn,principal,row,conversation_id,action)
    raise ValueError('Core plugin has no executor')
