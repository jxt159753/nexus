"""Owner-only plugin lifecycle and authenticated artifact downloads."""

from uuid import UUID
from typing import Literal
from fastapi import APIRouter, Request, Response
from pydantic import Field, StrictStr, StrictBool,StrictInt
from nexus.contracts.base import StrictContract
from nexus.plugins.contracts import PluginRelease
from nexus.core.harness.http import body
from nexus.core.plugins.service import public_version
from nexus.core.storage.database import one


class Install(StrictContract):
    request_id: UUID
    conversation_id: UUID
    release: PluginRelease


class GitHubInstall(StrictContract):
    request_id: UUID
    conversation_id: UUID
    repository: StrictStr=Field(max_length=200)
    commit: StrictStr=Field(pattern=r'^[a-f0-9]{40}$')
    path: StrictStr=Field(max_length=200)
    sha256: StrictStr=Field(pattern=r'^[a-f0-9]{64}$')


class Decision(StrictContract):
    conversation_id: UUID
    digest: StrictStr=Field(pattern=r'^[a-f0-9]{64}$')
    action: Literal['approve','enable','disable']


class TestRequest(StrictContract):
    conversation_id: UUID
    digest: StrictStr=Field(pattern=r'^[a-f0-9]{64}$')


class PlanChange(StrictContract):
    expected_revision: StrictInt=Field(ge=1)
    enabled: StrictBool


class RecordControl(StrictContract):
    request_id: UUID
    changes: dict=Field(default_factory=dict)


class PluginHTTP:
    def __init__(self,service,guards):
        self.service,self.guards=service,guards
        self.router=APIRouter(prefix='/api/v1/plugins')
        self.router.add_api_route('',self.page,methods=['GET'])
        self.router.add_api_route('',self.install,methods=['POST'])
        self.router.add_api_route('/github',self.github,methods=['POST'])
        self.router.add_api_route('/automation',self.automation,methods=['GET'])
        self.router.add_api_route('/conversations/{conversation_id}',self.conversation_state,methods=['GET'])
        self.router.add_api_route('/conversations/{conversation_id}/plan',self.plan,methods=['PUT'])
        self.router.add_api_route('/conversations/{conversation_id}/schedules/{record_id}',self.control_schedule,methods=['PATCH','DELETE'])
        self.router.add_api_route('/conversations/{conversation_id}/goals/{record_id}',self.control_goal,methods=['PATCH'])
        self.router.add_api_route('/versions/{version_id}',self.detail,methods=['GET'])
        self.router.add_api_route('/versions/{version_id}/decision',self.decide,methods=['POST'])
        self.router.add_api_route('/versions/{version_id}/test',self.test,methods=['POST'])
        self.router.add_api_route('/deliverables/{deliverable_id}',self.download,methods=['GET'])
        self.router.add_api_route('/transfers/{invocation_id}/{index}',self.transfer,methods=['POST'])

    async def page(self,request:Request):
        principal=await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.owner(conn,principal)
            rows=await conn.execute_fetchall('SELECT * FROM plugin_versions WHERE owner_id=? ORDER BY rowid DESC LIMIT 256',(str(principal.identity_id),))
            from nexus.core.plugins.ui_extension_management import catalog
            ui_plugins = await catalog(self.service.ui_extensions, conn, principal)
            return {'enabled':self.service.config.enabled,'plugins':[public_version(row) for row in rows], 'ui_plugins': ui_plugins}

    async def detail(self,version_id:UUID,request:Request):
        principal=await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.owner(conn,principal)
            row=await one(conn,'SELECT * FROM plugin_versions WHERE owner_id=? AND version_id=?',(str(principal.identity_id),str(version_id)))
            if row is None: raise LookupError('plugin version not found')
            return public_version(row,True)

    async def install(self,request:Request):
        principal=await self.guards.owner_http(request)
        value=await body(request,Install,max_bytes=32768)
        return await self.service.install(principal,value.conversation_id,value.request_id,value.release)

    async def decide(self,version_id:UUID,request:Request):
        principal=await self.guards.owner_http(request)
        value=await body(request,Decision)
        self.service.require_enabled()
        async with self.service.chat.harness.operation,self.service.database.transaction() as conn:
            await self.service.chat.conversation(conn,principal,value.conversation_id)
            row=await one(conn,'SELECT * FROM plugin_versions WHERE version_id=? AND owner_id=? AND digest=?',(str(version_id),str(principal.identity_id),value.digest))
            if row is None: raise LookupError('plugin version changed')
            return await self.service.decide(conn,principal,row,value.conversation_id,value.action)

    async def github(self,request:Request):
        from fastapi import HTTPException
        principal=await self.guards.owner_http(request)
        value=await body(request,GitHubInstall)
        self.service.require_enabled()
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.conversation(conn,principal,value.conversation_id)
        from nexus.core.plugins.sources import github
        try: release=await github(value.repository,value.commit,value.path,value.sha256)
        except (OSError,ValueError): raise HTTPException(400,detail='PLUGIN_SOURCE_INVALID') from None
        return await self.service.install(principal,value.conversation_id,value.request_id,release,f'github:{value.repository}@{value.commit}:{value.path}')

    async def conversation_state(self,conversation_id:UUID,request:Request):
        import json
        principal=await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            conversation=await self.service.chat.conversation(conn,principal,conversation_id)
            todos=await one(conn,'SELECT * FROM plugin_todos WHERE conversation_id=?',(str(conversation_id),))
            goals=await conn.execute_fetchall('SELECT * FROM plugin_goals WHERE conversation_id=? ORDER BY rowid DESC LIMIT 20',(str(conversation_id),))
            schedules=await conn.execute_fetchall('SELECT * FROM plugin_schedules WHERE conversation_id=? AND deleted=0 ORDER BY rowid DESC LIMIT 100',(str(conversation_id),))
            events=await conn.execute_fetchall('SELECT e.*,s.title,s.prompt FROM plugin_schedule_events e JOIN plugin_schedules s USING(schedule_id) WHERE s.conversation_id=? ORDER BY e.rowid DESC LIMIT 100',(str(conversation_id),))
            from nexus.core.plugins.schedule import public
            return {'plan_mode':bool(conversation['plan_mode']),'revision':conversation['revision'],'todos':json.loads(todos['value_json']) if todos else [],'todo_revision':todos['revision'] if todos else 0,'goals':[dict(row) | ({'expires_at':None,'max_rounds':None} if row['run_until_stopped'] else {}) for row in goals],
                    'schedules':[public(row) for row in schedules],'events':[dict(row) for row in events]}

    async def automation(self,request:Request):
        principal=await self.guards.owner_http(request)
        from nexus.core.plugins.schedule import public
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.owner(conn,principal)
            schedules=await conn.execute_fetchall('SELECT * FROM plugin_schedules WHERE owner_id=? AND deleted=0 ORDER BY rowid DESC LIMIT 100',(str(principal.identity_id),))
            goals=await conn.execute_fetchall('SELECT * FROM plugin_goals WHERE owner_id=? ORDER BY rowid DESC LIMIT 100',(str(principal.identity_id),))
            return {'schedules':[public(row) for row in schedules], 'goals':[dict(row) | ({'expires_at':None,'max_rounds':None} if row['run_until_stopped'] else {}) for row in goals]}

    async def plan(self,conversation_id:UUID,request:Request):
        from nexus.core.storage.tasks import Conflict
        principal=await self.guards.owner_http(request)
        value=await body(request,PlanChange)
        async with self.service.chat.harness.operation,self.service.database.transaction() as conn:
            conversation=await self.service.chat.conversation(conn,principal,conversation_id)
            if conversation['revision']!=value.expected_revision: raise Conflict('IDEMPOTENCY_CONFLICT')
            await conn.execute('UPDATE conversations SET plan_mode=?,revision=revision+1 WHERE conversation_id=?',(int(value.enabled),str(conversation_id)))
            return {'plan_mode':value.enabled,'revision':value.expected_revision+1}

    async def test(self,version_id:UUID,request:Request):
        principal=await self.guards.owner_http(request)
        value=await body(request,TestRequest)
        async with self.service.database.transaction(write=False) as conn:
            row=await one(conn,'SELECT 1 FROM plugin_versions WHERE version_id=? AND owner_id=? AND digest=?',(str(version_id),str(principal.identity_id),value.digest))
            if row is None: raise LookupError('plugin version changed')
        return await self.service.tester.start(principal,value.conversation_id,version_id)

    async def control_schedule(self,conversation_id:UUID,record_id:UUID,request:Request):
        from fastapi import HTTPException
        try: return await self.control_record(conversation_id,record_id,request,'schedule')
        except ValueError: raise HTTPException(400,detail='PLUGIN_CONTROL_INVALID') from None

    async def control_goal(self,conversation_id:UUID,record_id:UUID,request:Request):
        from fastapi import HTTPException
        try: return await self.control_record(conversation_id,record_id,request,'goal')
        except ValueError: raise HTTPException(400,detail='PLUGIN_CONTROL_INVALID') from None

    async def control_record(self,conversation_id,record_id,request,kind):
        import json
        from nexus.contracts.extensions import canonical
        from nexus.core.storage.database import utc_now,timestamp
        from nexus.core.storage.tasks import Conflict
        from nexus.core.security.types import AccessDenied
        principal=await self.guards.owner_http(request)
        value=await body(request,RecordControl)
        encoded=canonical({'kind':kind,'method':request.method,'conversation_id':str(conversation_id),**value.model_dump(mode='json')})
        async with self.service.chat.harness.operation,self.service.database.transaction() as conn:
            await self.service.chat.conversation(conn,principal,conversation_id)
            old=await one(conn,'SELECT * FROM plugin_control_requests WHERE request_id=?',(str(value.request_id),))
            if old:
                if old['owner_id']!=str(principal.identity_id) or old['target_id']!=str(record_id) or old['request_json']!=encoded: raise Conflict('IDEMPOTENCY_CONFLICT')
                return json.loads(old['result_json'])
            if (await one(conn,'SELECT count(*) FROM plugin_control_requests'))[0]>=10000: raise AccessDenied('PLUGIN_CONTROL_STORAGE_FULL')
            now=utc_now()
            if kind=='schedule':
                from nexus.core.plugins.schedule import manage
                from nexus.plugins.schema import checked
                from nexus.plugins.catalog import definition
                name='schedule_delete' if request.method=='DELETE' else 'schedule_update'
                if 'schedule_id' in value.changes: raise ValueError('schedule identity is part of the route')
                args={**value.changes,'schedule_id':str(record_id)}
                checked(definition('builtin.schedule',name).input_schema,args)
                result=await manage(conn,principal,str(conversation_id),name,args,now)
                resume=value.changes.get('paused') is False
            else:
                row=await one(conn,'SELECT * FROM plugin_goals WHERE goal_id=? AND conversation_id=? AND owner_id=?',(str(record_id),str(conversation_id),str(principal.identity_id)))
                if row is None: raise LookupError('goal not found')
                if set(value.changes)!={'status'} or value.changes['status'] not in {'paused','active'}: raise ValueError('goal control requires active/paused status')
                if row['status']=='complete' or not row['run_until_stopped'] and (row['expires_at']<=timestamp(now) or self.service.chat.config.call_limits_enabled and row['rounds']>=row['max_rounds']): raise Conflict('goal has ended')
                await conn.execute('UPDATE plugin_goals SET status=? WHERE goal_id=?',(value.changes['status'],str(record_id)))
                resume=value.changes['status']=='active'
                result={'goal_id':str(record_id),'status':value.changes['status']}
            authority=await one(conn,'SELECT * FROM plugin_authorities WHERE authority_id=?',(str(record_id),))
            if authority and resume:
                from nexus.core.plugins.lifecycle import previous_round,budget
                if authority['state']!='active': await previous_round(conn,authority)
                from nexus.core.security.modes import effective
                from nexus.core.security.service import scope_digest
                from nexus.core.cognitive.work_catalog import catalog
                from nexus.contracts.extensions import digest_value
                current=await catalog(self.service.chat,conn,principal,UUID(authority['node_id']))
                if scope_digest(self.service.chat.harness.scope(UUID(authority['node_id'])))!=authority['scope_digest'] or digest_value(current['catalog'])!=authority['catalog_digest']:
                    raise AccessDenied('PLUGIN_AUTONOMY_SCOPE_CHANGED')
                mode=await effective(conn,str(conversation_id))
                cursor=(await one(conn,"SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
                await conn.execute("UPDATE plugin_authorities SET state='active',permission_fingerprint=?,cancel_cursor=? WHERE authority_id=?",(mode['fingerprint'],cursor,str(record_id)))
                if authority['state']!='active': await budget(conn,self.service.chat,authority['last_turn_id'],self.service.chat.config.model_reservation())
            elif authority:
                await conn.execute("UPDATE plugin_authorities SET state='paused' WHERE authority_id=?",(str(record_id),))
                turn=await one(conn,'SELECT status,work_status FROM chat_turns WHERE turn_id=?',(authority['last_turn_id'],))
                wakeup=await one(conn,'SELECT 1 FROM plugin_wakeups WHERE turn_id=?',(authority['last_turn_id'],))
                if turn and (kind=='goal' or wakeup) and (turn['status'] in {'queued','generating'} or turn['work_status']):
                    await self.service.chat.work.abort(conn,authority['last_turn_id'],'PLUGIN_AUTONOMY_PAUSED',now)
            from nexus.core.storage.database import audit
            await audit(conn,str(principal.identity_id),None,'plugin.control',now)
            await conn.execute('INSERT INTO plugin_control_requests VALUES(?,?,?,?,?,?)',(str(value.request_id),str(principal.identity_id),str(record_id),encoded,canonical(result),timestamp(now)))
            return result

    async def download(self,deliverable_id:UUID,request:Request):
        from urllib.parse import quote
        principal=await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.owner(conn,principal)
            row=await one(conn,"SELECT d.* FROM plugin_deliverables d JOIN tasks t USING(task_id) WHERE d.deliverable_id=? AND d.owner_id=? AND t.status='succeeded' AND t.execution_outcome='known'",(str(deliverable_id),str(principal.identity_id)))
            if row is None: raise LookupError('deliverable not found')
            return Response(row['content'],media_type='application/octet-stream',headers={'Content-Disposition':"attachment; filename=download; filename*=UTF-8''"+quote(row['name'],safe=''),'X-Content-SHA256':row['sha256']})

    async def transfer(self,invocation_id:UUID,index:int,request:Request):
        import hashlib,json,re
        import asyncio
        from pathlib import PurePosixPath
        from uuid import uuid5,NAMESPACE_URL
        from pydantic import SecretStr
        from nexus.core.security.types import AccessDenied
        from nexus.core.security.work import allowed_step
        from nexus.core.storage.database import timestamp,utc_now
        self.service.require_enabled()
        self.guards._transport(request,origin=False)
        authorization=request.headers.get('authorization','')
        if not authorization.startswith('Bearer '): raise AccessDenied('AUTH_REQUIRED')
        principal=await self.guards.service.authenticate_token(SecretStr(authorization[7:]),'node')
        digest=request.headers.get('x-content-sha256','')
        if not 0<=index<4 or not re.fullmatch(r'[a-f0-9]{64}',digest): raise ValueError('invalid artifact headers')
        content=bytearray()
        async with asyncio.timeout(8):
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content)>4*1024*1024: raise ValueError('artifact byte limit')
        if hashlib.sha256(content).hexdigest()!=digest: raise ValueError('artifact digest mismatch')
        async with self.service.chat.harness.operation,self.service.database.transaction() as conn:
            step,grant=await allowed_step(conn,str(invocation_id),utc_now())
            if grant['node_id']!=str(principal.identity_id): raise AccessDenied()
            args=json.loads(step['proposal_json'])['arguments']
            kind=(args.get('plugin_id'),args.get('tool'))
            if kind not in {('builtin.present','present'),('builtin.mcp','mcp_call_tool')}: raise AccessDenied()
            task=await one(conn,'SELECT t.* FROM tasks t JOIN task_attempts a ON a.attempt_id=t.current_attempt_id WHERE t.task_id=? AND a.node_id=? AND a.phase IN (\'dispatching\',\'running\')',(step['task_id'],str(principal.identity_id)))
            if task is None: raise AccessDenied('ARTIFACT_TASK_NOT_RUNNING')
            if kind==('builtin.mcp','mcp_call_tool'):
                from nexus.core.cognitive.attachment_media import image_media_type
                from nexus.plugins.mcp_images import IMAGE_TYPES
                media=image_media_type(content)
                if media not in IMAGE_TYPES: raise ValueError('invalid MCP image bytes')
                item={'path':f'mcp-image-{index+1}.{IMAGE_TYPES[media]}','description':'MCP 返回的图片'}
            else:
                item=args['input']['files'][index]
            identifier=str(uuid5(NAMESPACE_URL,f'{invocation_id}:{index}'))
            existing=await one(conn,'SELECT sha256 FROM plugin_deliverables WHERE deliverable_id=?',(identifier,))
            if existing and existing[0]!=digest: raise ValueError('artifact identity conflict')
            used=await one(conn,'SELECT count(*),coalesce(sum(length(content)),0) FROM plugin_deliverables')
            if not existing and (used[0]>=1000 or used[1]+len(content)>64*1024*1024): raise ValueError('artifact storage full')
            name=PurePosixPath(item['path'].replace('\\','/')).name
            await conn.execute('INSERT OR IGNORE INTO plugin_deliverables VALUES(?,?,?,?,?,?,?,?,?)',
                (identifier,grant['owner_id'],grant['conversation_id'],step['task_id'],name,item.get('description',''),digest,bytes(content),timestamp(utc_now())))
            return {'deliverable_id':identifier,'name':name,'description':item.get('description',''),'size_bytes':len(content),'sha256':digest,'download_url':'/api/v1/plugins/deliverables/'+identifier}
