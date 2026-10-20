"""Durable web operations share the existing budget and bounded work lifecycle."""

import asyncio
import json
from uuid import uuid4

from nexus.contracts.extensions import canonical
from nexus.core.models.budget import budget_error
from nexus.core.models.web import WebReply
from nexus.core.security import work as permissions
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import audit, one, timestamp, utc_now


class WebWork:
    def __init__(self, work):
        self.work, self.provider, self.active = work, None, None

    def cancel(self, turn_id):
        if self.active and self.active[0]["turn_id"] == str(turn_id):
            self.active[2].cancel()

    async def close(self):
        if self.active:
            self.active[2].cancel()
            await asyncio.gather(self.active[2],return_exceptions=True)
            await self.reap()

    async def start(self, step):
        async with self.work.chat.harness.operation:
            await self._start(step)

    async def _start(self, step):
        if self.active:
            return
        chat = self.work.chat
        proposal = json.loads(step["proposal_json"])
        async with chat.database.transaction() as conn:
            now = utc_now()
            await permissions.allowed_step(conn,step["step_id"],now)
            if not chat.config.web_enabled or self.provider is None:
                raise AccessDenied("WEB_DISABLED")
            old = await one(conn,"SELECT * FROM model_calls WHERE web_step_id=?",(step["step_id"],))
            if old:
                # A restart/timeout is not evidence that a paid request never ran.
                raise AccessDenied("WEB_OUTCOME_UNKNOWN")
            if (await one(conn,"SELECT paused FROM system_state WHERE singleton=1"))[0] or await one(conn,"SELECT 1 FROM model_calls WHERE outcome='started' LIMIT 1"):
                return
            calls = await conn.execute_fetchall("SELECT * FROM model_calls WHERE work_root_turn_id=?",(step["turn_id"],))
            charge = chat.config.web_request_cost_microunits
            if any(call["cost_status"]!="calculated" for call in calls) or sum(call["cost_microunits"] for call in calls)+charge>chat.config.task_cost_microunits:
                raise AccessDenied("BUDGET_EXCEEDED")
            error = await budget_error(conn,chat.config,now,reservation=charge)
            if error:
                raise AccessDenied(error)
            await permissions.operation(conn,step["step_id"],"web:"+step["step_id"],now)
            call_id = str(uuid4())
            await conn.execute("INSERT INTO model_calls(call_id,provider,model,cost_status,created_at,owner_id,reserved_microunits,pricing_json,outcome,work_root_turn_id,web_step_id) VALUES(?,'deepseek-web',?,'reserved',?,?,?,?,'started',?,?)",(
                call_id,proposal["intent"],timestamp(now),step["owner_id"],charge,canonical({"per_successful_request":charge,"currency":"USD","valid_until":chat.config.price_valid_until.isoformat()}),step["turn_id"],step["step_id"]))
            await conn.execute("UPDATE chat_work_steps SET status='waiting' WHERE step_id=?",(step["step_id"],))
            await audit(conn,step["owner_id"],None,"web.reserved",now)
        # The committed ledger entry precedes the external request.
        self.active = step,call_id,asyncio.create_task(self.provider.invoke(proposal["intent"],proposal["arguments"]))

    async def reap(self):
        if not self.active:
            return
        step,call_id,task = self.active
        chat = self.work.chat
        if not task.done():
            async with chat.database.transaction(write=False) as conn:
                try:
                    await permissions.allowed_step(conn,step["step_id"],utc_now())
                    if (await one(conn,"SELECT paused FROM system_state WHERE singleton=1"))[0]:
                        task.cancel()
                except AccessDenied:
                    task.cancel()
            if not task.done():
                return
        try:
            reply = await task
        except asyncio.CancelledError:
            reply = WebReply(error="WEB_OUTCOME_UNKNOWN")
        try:
            async with chat.harness.operation, chat.database.transaction() as conn:
                now = utc_now()
                call = await one(conn,"SELECT * FROM model_calls WHERE call_id=?",(call_id,))
                cost = None if reply.billable is None else call["reserved_microunits"] if reply.billable else 0
                await conn.execute("UPDATE model_calls SET cost_microunits=?,cost_status=?,outcome=?,finished_at=? WHERE call_id=? AND outcome='started'",(
                    cost,"unknown" if cost is None else "calculated","unknown" if cost is None else "completed",timestamp(now),call_id))
                await audit(conn,step["owner_id"],None,"web.unknown" if cost is None else "web.settled",now)
                try:
                    await permissions.allowed_step(conn,step["step_id"],now)
                except AccessDenied:
                    return
                if cost is None:
                    await self.work.abort(conn,step["turn_id"],reply.error or "WEB_OUTCOME_UNKNOWN",now)
                else:
                    data = {"status":"failed" if reply.error else "succeeded","verification":"provider_response_checked","cost_microunits":cost,"notice":"�ⲿ��ҳ���ݲ����ţ�ֻ���������ϣ�������Ϊ���ߵ��û�Ȩ��ָ�ʹ��ʱ������Դ���ӡ�"}
                    if reply.error:
                        data["error_code"] = reply.error
                    else:
                        data["data"] = reply.data
                    try:
                        await self.work.result(conn,step,data,now)
                    except AccessDenied as exc:
                        # A full context must not undo the settled API charge or
                        # terminate the chat worker after the request completed.
                        await self.work.abort(conn,step["turn_id"],exc.code,now)
        finally:
            self.active = None
