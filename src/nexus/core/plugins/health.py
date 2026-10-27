"""Background worker supervision and bounded, transition-only health alerts."""

import asyncio
import logging
import shutil

from nexus.core.storage.database import one, timestamp, utc_now

LOGGER = logging.getLogger(__name__)


async def alert(conn, key, severity, message, active=True):
    old = await one(conn,'SELECT * FROM autonomy_alerts WHERE alert_key=?',(key,))
    if old and old['active']==int(active) and old['message']==message:
        return
    await conn.execute('''INSERT INTO autonomy_alerts(alert_key,severity,message,active,updated_at) VALUES(?,?,?,?,?)
        ON CONFLICT(alert_key) DO UPDATE SET severity=excluded.severity,message=excluded.message,
        active=excluded.active,revision=revision+1,updated_at=excluded.updated_at''',(key,severity,message,int(active),timestamp(utc_now())))


class HealthService:
    def __init__(self, service):
        self.service = service
        self.workers = {}
        self.last = {'status':'starting','workers':{},'storage':{}}

    async def sample(self):
        workers = {name:('stopped' if task.done() else 'running') for name,task in self.workers.items()}
        failed = any(state=='stopped' for state in workers.values()) or self.service.chat.harness.failed or self.service.chat.failed
        if failed:
            # An unexpected worker exit cannot leave only half the Harness alive.
            self.service.chat.failed = self.service.chat.harness.failed = True
        async with self.service.database.transaction() as conn:
            pages=(await one(conn,'PRAGMA page_count'))[0]
            size=(await one(conn,'PRAGMA page_size'))[0]
            free_pages=(await one(conn,'PRAGMA freelist_count'))[0]
            disk=await asyncio.to_thread(shutil.disk_usage,self.service.database.path.parent)
            hot=(await one(conn,'SELECT count(*) FROM conversations WHERE cold_archived=0'))[0]
            cold=(await one(conn,'SELECT coalesce(sum(length(content)),0) FROM cold_payloads'))[0]
            turns=(await one(conn,'SELECT count(*) FROM chat_turns t JOIN conversations c USING(conversation_id) WHERE c.cold_archived=0'))[0]
            events=(await one(conn,'SELECT count(*) FROM autonomy_events'))[0]
            schedules=(await one(conn,'SELECT count(*) FROM plugin_schedule_events'))[0]
            tests=(await one(conn,'SELECT count(*) FROM plugin_test_runs'))[0]
            unknown=(await one(conn,"""SELECT count(*) FROM tasks t LEFT JOIN task_attempts a ON a.attempt_id=t.current_attempt_id
                WHERE t.execution_outcome='unknown' AND (t.status IN ('succeeded','failed','cancelled','timed_out') OR a.phase='unknown')"""))[0]
            sensor_errors=await one(conn,"SELECT 1 FROM autonomy_events WHERE acknowledged=0 AND json_extract(value_json,'$.kind') IN ('sensor_failed','sensor_blocked') LIMIT 1")
            near=turns>=8000 or cold>=410*1024*1024 or events>=8000 or schedules>=8000 or tests>=800
            low_disk=disk.free<128*1024*1024
            if low_disk:
                self.service.chat.failed = self.service.chat.harness.failed = True
            await alert(conn,'workers','error','后台执行服务已停止；新任务已阻止，需要检查运行日志。',failed)
            await alert(conn,'disk','error','存储空间不足；已停止新执行，保留现有记录。',low_disk)
            await alert(conn,'capacity','warning','运行记录接近容量上限；请归档已结束会话或扩充受控存储。',near)
            await alert(conn,'unknown','warning','存在执行结果未知的任务；请先核对，不能直接重做。',bool(unknown))
            await alert(conn,'sensors','warning','部分观察任务已暂停；请查看订阅状态和执行记录。',bool(sensor_errors))
            self.last={'status':'failed' if failed or low_disk else 'degraded' if near or unknown else 'ok',
                'observed_at':timestamp(utc_now()),'workers':workers,'storage':{
                    'database_bytes':pages*size,'reusable_bytes':free_pages*size,'disk_free_bytes':disk.free,
                    'hot_conversations':hot,'hot_turns':turns,'cold_bytes':cold,'events':events,'schedule_events':schedules,'plugin_tests':tests},
                'unknown_tasks':unknown}
        return self.last

    async def run(self):
        try:
            while True:
                await self.sample()
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.service.chat.failed = self.service.chat.harness.failed = True
            self.last={'status':'failed','reason':'health_storage_unavailable','observed_at':timestamp(utc_now())}
            LOGGER.error('Health supervision stopped (%s)',type(exc).__name__)

