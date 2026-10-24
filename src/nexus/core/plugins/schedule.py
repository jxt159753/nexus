"""Finite reminder scheduling, deterministic UTC dispatch and durable deduplication."""

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nexus.contracts.extensions import canonical
from nexus.core.storage.database import one, timestamp, utc_now


def zone(name):
    try: return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name=='UTC': return UTC
        if name=='Asia/Shanghai': return timezone(timedelta(hours=8))
        raise ValueError('time zone data unavailable') from None


def cron_field(expression,low,high):
    output=set()
    for item in expression.split(','):
        parts=item.split('/')
        if len(parts)>2: raise ValueError('invalid cron field')
        step=int(parts[1]) if len(parts)==2 else 1
        if step<1: raise ValueError('cron step must be positive')
        if parts[0]=='*': start,end=low,high
        elif '-' in parts[0]: start,end=map(int,parts[0].split('-'))
        else: start=end=int(parts[0])
        if not low<=start<=end<=high: raise ValueError('cron value out of range')
        output.update(range(start,end+1,step))
    return output


def next_time(rule,after):
    if 'after_seconds' in rule: return after+timedelta(seconds=rule['after_seconds'])
    if 'at' in rule:
        value=datetime.fromisoformat(rule['at'].replace('Z','+00:00'))
        if value.tzinfo is None: raise ValueError('absolute schedule time requires an offset')
        value=value.astimezone(UTC)
        return value if value>after else None
    if 'every_seconds' in rule: return after+timedelta(seconds=rule['every_seconds'])
    local_zone=zone(rule.get('time_zone','Asia/Shanghai'))
    local=after.astimezone(local_zone)
    if 'daily' in rule:
        parts=list(map(int,rule['daily'].split(':')))
        if len(parts) not in (2,3): raise ValueError('daily time requires HH:mm or HH:mm:ss')
        for delta in range(32):
            day=(local+timedelta(days=delta)).date()
            candidate=datetime(day.year,day.month,day.day,*parts,tzinfo=local_zone)
            if candidate>local and (not rule.get('weekdays') or candidate.isoweekday() in rule['weekdays']): return candidate.astimezone(UTC)
        return None
    fields=rule['cron'].split()
    if len(fields)!=5: raise ValueError('cron requires five fields')
    minutes,hours,days,months,weekdays=[cron_field(field,low,high) for field,(low,high) in zip(fields,[(0,59),(0,23),(1,31),(1,12),(0,7)])]
    candidate=after.astimezone(UTC).replace(second=0,microsecond=0)+timedelta(minutes=1)
    for _ in range(44640):
        view=candidate.astimezone(local_zone)
        week=(view.weekday()+1)%7
        day_matches=view.day in days; week_matches=week in weekdays or week==0 and 7 in weekdays
        # Vixie cron: restricted day-of-month and day-of-week use OR.
        date_matches=(day_matches or week_matches) if fields[2]!='*' and fields[4]!='*' else day_matches and week_matches
        if view.minute in minutes and view.hour in hours and view.month in months and date_matches: return candidate
        candidate+=timedelta(minutes=1)
    # A valid rule can have no occurrence in the bounded schedule lifetime.
    return None


async def manage(conn,principal,conversation_id,name,value,now):
    if name=='schedule_list':
        rows=await conn.execute_fetchall('SELECT * FROM plugin_schedules WHERE owner_id=? AND conversation_id=? AND deleted=0 ORDER BY rowid DESC LIMIT 100',(str(principal.identity_id),conversation_id))
        return {'schedules':[{key:row[key] for key in row.keys() if key!='rule_json'}|{'rule':json.loads(row['rule_json'])} for row in rows]}
    if name=='schedule_create':
        keys={'after_seconds','at','every_seconds','daily','cron'}&set(value)
        if len(keys)!=1: raise ValueError('select exactly one schedule rule')
        if value.get('weekdays') and 'daily' not in value: raise ValueError('weekdays requires daily time')
        if (await one(conn,'SELECT count(*) FROM plugin_schedules'))[0]>=1000: raise ValueError('schedule storage full')
        rule={key:value[key] for key in keys|({'time_zone','weekdays'}&set(value))}
        due=next_time(rule,now)
        expiry=now+timedelta(days=value.get('duration_days',7))
        if due is None or due>expiry: raise ValueError('schedule has no occurrence before expiry')
        identifier=str(uuid4())
        runs=value.get('max_runs',1 if 'at' in keys or 'after_seconds' in keys else 10)
        await conn.execute('INSERT INTO plugin_schedules VALUES(?,?,?,?,?,?,?,?,?,0,0,0,?)',
            (identifier,conversation_id,str(principal.identity_id),value.get('title',value['prompt'][:80]),value['prompt'],canonical(rule),timestamp(due),timestamp(expiry),runs,timestamp(now)))
        return {'schedule_id':identifier,'next_at':timestamp(due),'max_runs':runs,'expires_at':timestamp(expiry),'delivery':'conversation_event','model_calls_per_reminder':0}
    row=await one(conn,'SELECT * FROM plugin_schedules WHERE schedule_id=? AND conversation_id=? AND owner_id=? AND deleted=0',(value['schedule_id'],conversation_id,str(principal.identity_id)))
    if row is None: raise LookupError('schedule not found')
    if name=='schedule_delete':
        await conn.execute('UPDATE plugin_schedules SET deleted=1,paused=1 WHERE schedule_id=?',(row['schedule_id'],))
        await conn.execute("UPDATE plugin_authorities SET state='paused' WHERE authority_id=?",(row['schedule_id'],))
        return {'schedule_id':row['schedule_id'],'deleted':True}
    if 'at' in value and 'every_seconds' in value: raise ValueError('select one replacement rule')
    rule=json.loads(row['rule_json'])
    if 'at' in value: rule={'at':value['at']}
    if 'every_seconds' in value: rule={'every_seconds':value['every_seconds']}
    due=next_time(rule,now) if 'at' in value or 'every_seconds' in value else datetime.fromisoformat(row['next_at'])
    if due is None or due>datetime.fromisoformat(row['expires_at']): raise ValueError('updated schedule is already ended')
    await conn.execute('UPDATE plugin_schedules SET title=?,prompt=?,rule_json=?,next_at=?,paused=? WHERE schedule_id=?',
        (value.get('title',row['title']),value.get('prompt',row['prompt']),canonical(rule),timestamp(due),int(value.get('paused',bool(row['paused']))),row['schedule_id']))
    if value.get('paused') is True: await conn.execute("UPDATE plugin_authorities SET state='paused' WHERE authority_id=?",(row['schedule_id'],))
    return {'schedule_id':row['schedule_id'],'updated':True,'next_at':timestamp(due)}


async def tick(service,now=None):
    now=now or utc_now()
    async with service.chat.harness.operation,service.database.transaction() as conn:
        if (await one(conn,'SELECT paused FROM system_state WHERE singleton=1'))[0]: return
        rows=await conn.execute_fetchall("SELECT s.* FROM plugin_schedules s JOIN conversations c USING(conversation_id) JOIN identities i ON i.identity_id=s.owner_id WHERE s.deleted=0 AND s.paused=0 AND s.next_at<=? AND c.archived=0 AND i.revoked_at IS NULL LIMIT 100",(timestamp(now),))
        for row in rows:
            expiry=datetime.fromisoformat(row['expires_at'])
            due=datetime.fromisoformat(row['next_at'])
            if row['runs']>=row['max_runs'] or now>expiry:
                await conn.execute('UPDATE plugin_schedules SET paused=1 WHERE schedule_id=?',(row['schedule_id'],)); continue
            if (await one(conn,'SELECT count(*) FROM plugin_schedule_events'))[0]>=10000: raise OSError('schedule event journal full')
            status='missed' if (now-due).total_seconds()>300 else 'due'
            await conn.execute('INSERT OR IGNORE INTO plugin_schedule_events VALUES(?,?,?,?,?)',(str(uuid4()),row['schedule_id'],row['next_at'],status,timestamp(now)))
            rule=json.loads(row['rule_json'])
            next_at=None if row['runs']+1>=row['max_runs'] or now>=expiry or 'at' in rule or 'after_seconds' in rule else next_time(rule,now)
            ended=next_at is None or next_at>expiry or row['runs']+1>=row['max_runs']
            await conn.execute('UPDATE plugin_schedules SET runs=runs+1,next_at=?,paused=? WHERE schedule_id=?',(timestamp(next_at) if next_at else row['next_at'],int(ended),row['schedule_id']))


async def run(service):
    while True:
        if service.config.enabled:
            from nexus.core.plugins.observations import tick as observe
            from nexus.core.plugins.business import observe as evaluate_rollouts
            await observe(service)
            await evaluate_rollouts(service)
            from nexus.core.plugins.mcp import poll as poll_mcp
            await poll_mcp(service)
            await tick(service)
            from nexus.core.plugins.lifecycle import tick as continue_work
            await continue_work(service)
        await asyncio.sleep(1)
