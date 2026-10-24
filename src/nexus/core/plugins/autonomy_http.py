"""Owner-only control of autonomous plans, sensors, test gates and archives."""

from uuid import UUID
from fastapi import APIRouter, Request
from pydantic import Field, StrictInt, StrictStr

from nexus.contracts.autonomy import GoalPlan, GoalCheckpoint, GoalPolicy
from nexus.contracts.base import StrictContract
from nexus.core.harness.http import body
from nexus.core.plugins import goals, observations, business
from nexus.core.storage import archive
from nexus.core.storage.database import one, audit, utc_now
from nexus.core.storage.tasks import Conflict


class ArchiveConfirm(StrictContract):
    digest: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')


class Revision(StrictContract):
    expected_revision: StrictInt = Field(ge=1)


class AutonomyHTTP:
    def __init__(self, service, guards):
        self.service, self.guards = service, guards
        self.router = APIRouter(prefix='/api/v1/autonomy')
        for path, method, handler in [
            ('/health','GET',self.health),('/events','GET',self.events),('/events/{event_id}/ack','POST',self.ack),
            ('/watches','GET',self.watches),('/watches','POST',self.create_watch),('/watches/{watch_id}','DELETE',self.stop_watch),
            ('/watches/{watch_id}/events','POST',self.external),
            ('/conversations/{conversation_id}/goals/{goal_id}','GET',self.goal),
            ('/conversations/{conversation_id}/goals/{goal_id}/plan','PUT',self.plan),
            ('/conversations/{conversation_id}/goals/{goal_id}/checkpoints','POST',self.checkpoint),
            ('/conversations/{conversation_id}/goals/{goal_id}/policy','PUT',self.policy),
            ('/plugins/{plugin_id}/business-suite','PUT',self.suite),
            ('/plugins/{plugin_id}/business-suite','GET',self.suite_state),
            ('/conversations/{conversation_id}/archive-preview','GET',self.archive_preview),
            ('/conversations/{conversation_id}/archive','POST',self.archive),
        ]:
            self.router.add_api_route(path,handler,methods=[method])

    async def health(self, request: Request):
        principal=await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.owner(conn,principal)
            alerts=[dict(row) for row in await conn.execute_fetchall('SELECT * FROM autonomy_alerts ORDER BY updated_at DESC LIMIT 100')]
        return self.service.health.last | {'alerts':alerts}

    async def events(self, request: Request, after: int = 0):
        principal=await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.owner(conn,principal)
            rows=await conn.execute_fetchall('''SELECT e.*,w.kind,w.priority,w.conversation_id FROM autonomy_events e JOIN autonomy_watches w USING(watch_id)
                WHERE w.owner_id=? AND e.event_id>? ORDER BY e.event_id LIMIT 100''',(str(principal.identity_id),max(0,after)))
            return {'events':[dict(row) for row in rows],'next_after':rows[-1]['event_id'] if rows else after}

    async def ack(self,event_id:int,request:Request):
        principal=await self.guards.owner_http(request)
        async with self.service.database.transaction() as conn:
            await self.service.chat.owner(conn,principal)
            changed=await conn.execute('UPDATE autonomy_events SET acknowledged=1 WHERE event_id=? AND watch_id IN (SELECT watch_id FROM autonomy_watches WHERE owner_id=?)',(event_id,str(principal.identity_id)))
            if not changed.rowcount: raise LookupError('event not found')
        return {'acknowledged':True}

    async def watches(self,request:Request):
        principal=await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.owner(conn,principal)
            rows=await conn.execute_fetchall('SELECT * FROM autonomy_watches WHERE owner_id=? ORDER BY rowid DESC LIMIT 100',(str(principal.identity_id),))
        return {'watches':[dict(row) for row in rows]}

    async def create_watch(self,request:Request):
        principal=await self.guards.owner_http(request)
        value=await body(request,observations.WatchCreate)
        async with self.service.chat.harness.operation,self.service.database.transaction() as conn:
            return await observations.create(self.service,conn,principal,value)

    async def stop_watch(self,watch_id:UUID,request:Request):
        principal=await self.guards.owner_http(request); value=await body(request,Revision)
        async with self.service.chat.harness.operation,self.service.database.transaction() as conn:
            await self.service.chat.owner(conn,principal)
            watch=await one(conn,'SELECT * FROM autonomy_watches WHERE watch_id=? AND owner_id=?',(str(watch_id),str(principal.identity_id)))
            if not watch: raise LookupError('watch not found')
            if watch['revision']!=value.expected_revision: raise Conflict('watch changed')
            await conn.execute('UPDATE autonomy_watches SET paused=1,revision=revision+1 WHERE watch_id=?',(str(watch_id),))
            if watch['pending_task_id']:
                from nexus.core.storage.control import cancel_task
                from nexus.core.storage.tasks import load_task
                task=await load_task(conn,UUID(watch['pending_task_id']))
                if task.status not in {'succeeded','failed','cancelled','timed_out'}:
                    await cancel_task(conn,task,principal.identity_id,utc_now())
            await audit(conn,str(principal.identity_id),None,'watch.stopped',utc_now())
        return {'paused':True,'revision':value.expected_revision+1}

    async def external(self,watch_id:UUID,request:Request):
        principal=await self.guards.owner_http(request); value=await body(request,observations.ExternalEvent)
        async with self.service.chat.harness.operation,self.service.database.transaction() as conn:
            await self.service.chat.owner(conn,principal)
            watch=await one(conn,'SELECT * FROM autonomy_watches WHERE watch_id=? AND owner_id=?',(str(watch_id),str(principal.identity_id)))
            if not watch: raise LookupError('watch not found')
            await observations.external(self.service,conn,watch,value)
        return {'recorded':True}

    async def goal(self,conversation_id:UUID,goal_id:UUID,request:Request):
        principal=await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.conversation(conn,principal,conversation_id)
            await goals.owned(conn,principal.identity_id,conversation_id,goal_id)
            return await goals.snapshot(conn,goal_id)

    async def change_goal(self,conversation_id,goal_id,request,contract,action):
        principal=await self.guards.owner_http(request); value=await body(request,contract,max_bytes=16384)
        async with self.service.chat.harness.operation,self.service.database.transaction() as conn:
            await self.service.chat.conversation(conn,principal,conversation_id)
            goal=await goals.owned(conn,principal.identity_id,conversation_id,goal_id)
            return await action(conn,goal,value)

    async def plan(self,conversation_id:UUID,goal_id:UUID,request:Request):
        return await self.change_goal(conversation_id,goal_id,request,GoalPlan,goals.set_plan)

    async def checkpoint(self,conversation_id:UUID,goal_id:UUID,request:Request):
        return await self.change_goal(conversation_id,goal_id,request,GoalCheckpoint,goals.checkpoint)

    async def policy(self,conversation_id:UUID,goal_id:UUID,request:Request):
        return await self.change_goal(conversation_id,goal_id,request,GoalPolicy,goals.policy)

    async def suite(self,plugin_id:str,request:Request):
        principal=await self.guards.owner_http(request); value=await body(request,business.BusinessSuite,max_bytes=16384)
        async with self.service.chat.harness.operation,self.service.database.transaction() as conn:
            await self.service.chat.owner(conn,principal)
            return await business.configure(conn,principal.identity_id,plugin_id,value)

    async def suite_state(self,plugin_id:str,request:Request):
        principal=await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            await self.service.chat.owner(conn,principal)
            suite=await one(conn,'SELECT * FROM plugin_business_suites WHERE owner_id=? AND plugin_id=?',(str(principal.identity_id),plugin_id))
            rollouts=await conn.execute_fetchall('SELECT r.* FROM plugin_rollouts r JOIN plugin_versions v USING(version_id) WHERE r.owner_id=? AND v.plugin_id=? ORDER BY r.rowid DESC LIMIT 20',(str(principal.identity_id),plugin_id))
        return {'suite':dict(suite) if suite else None,'rollouts':[dict(row) for row in rollouts]}

    async def archive_preview(self,conversation_id:UUID,request:Request):
        principal=await self.guards.owner_http(request)
        async with self.service.database.transaction(write=False) as conn:
            conversation=await self.service.chat.conversation(conn,principal,conversation_id)
            return await archive.preview(conn,conversation)

    async def archive(self,conversation_id:UUID,request:Request):
        principal=await self.guards.owner_http(request); value=await body(request,ArchiveConfirm)
        async with self.service.chat.harness.operation,self.service.database.transaction() as conn:
            conversation=await self.service.chat.conversation(conn,principal,conversation_id)
            return await archive.archive(conn,conversation,value.digest)
