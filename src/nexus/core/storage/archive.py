"""Lossless cold payloads with transactional replacement and verified readback.

Identity, permissions, execution receipts and cost ledgers remain in their
original tables. Only ended, explicitly archived conversations are eligible.
"""

import hashlib
import json
import zlib

from nexus.contracts.extensions import canonical, digest_value
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now, audit
from nexus.core.storage.tasks import Conflict

FIELDS = {
    'chat_turns': ('turn_id', {'text':'', 'response_text':'', 'context_json':'[]', 'initial_response_text':None,
        'context_surface_json':None, 'work_messages_json':None, 'result_preview_json':None,
        'auto_result_json':None, 'skill_catalog_json':'[]', 'skill_plan_json':None, 'work_catalog_json':'[]'}),
    'chat_exchanges': ('call_id', {'request_body':'', 'response_json':None, 'partial_text':''}),
    'chat_compactions': ('call_id', {'source_messages_json':'', 'summary_text':''}),
}
MAX_ROW = 8 * 1024 * 1024
MAX_BATCH = 64 * 1024 * 1024
MAX_COLD = 512 * 1024 * 1024


def unpack(row):
    if not 0 <= row['original_bytes'] <= MAX_ROW:
        raise AccessDenied('ARCHIVE_INVALID')
    decoder = zlib.decompressobj()
    raw = decoder.decompress(row['content'], row['original_bytes']+1)
    if not decoder.eof or decoder.unused_data or len(raw) != row['original_bytes'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
        raise AccessDenied('ARCHIVE_INVALID')
    return json.loads(raw)


async def hydrate(conn, table, row):
    if row is None:
        return None
    key, fields = FIELDS[table]
    cold = await one(conn, 'SELECT * FROM cold_payloads WHERE table_name=? AND record_id=?', (table, row[key]))
    if not cold:
        return row
    value = unpack(cold)
    if set(value) != set(fields):
        raise AccessDenied('ARCHIVE_INVALID')
    return dict(row) | value


async def eligible(conn, conversation):
    from nexus.core.cognitive.chat import ACTIVE
    identifier = conversation['conversation_id']
    if not conversation['archived']:
        raise AccessDenied('ARCHIVE_CONVERSATION_FIRST')
    if await one(conn, f'SELECT 1 FROM chat_turns WHERE conversation_id=? AND {ACTIVE}', (identifier,)):
        raise AccessDenied('ARCHIVE_ACTIVE_WORK')
    if await one(conn, "SELECT 1 FROM plugin_authorities WHERE conversation_id=? AND state IN ('active','interrupted')", (identifier,)):
        raise AccessDenied('ARCHIVE_ACTIVE_WORK')
    if await one(conn, "SELECT 1 FROM autonomy_watches WHERE conversation_id=? AND paused=0 AND expires_at>?", (identifier,timestamp(utc_now()))):
        raise AccessDenied('ARCHIVE_ACTIVE_WORK')
    if await one(conn, "SELECT 1 FROM plugin_test_runs WHERE conversation_id=? AND status IN ('pending','running','unknown')", (identifier,)):
        raise AccessDenied('ARCHIVE_ACTIVE_WORK')
    if await one(conn, """SELECT 1 FROM model_calls m LEFT JOIN chat_compactions p USING(call_id)
        WHERE coalesce(m.chat_turn_id,m.work_root_turn_id,p.turn_id) IN
        (SELECT turn_id FROM chat_turns WHERE conversation_id=?) AND (m.outcome IN ('started','unknown') OR m.cost_status!='calculated')""", (identifier,)):
        raise AccessDenied('ARCHIVE_UNSETTLED_COST')
    if await one(conn, """WITH turns AS (SELECT turn_id,task_id,skill_run_id FROM chat_turns WHERE conversation_id=?),
        steps AS (SELECT step_id,task_id,skill_run_id FROM chat_work_steps WHERE turn_id IN (SELECT turn_id FROM turns)),
        related AS (SELECT task_id FROM turns UNION SELECT task_id FROM steps
            UNION SELECT k.task_id FROM chat_work_tasks k JOIN steps s USING(step_id)
            UNION SELECT task_id FROM skill_run_steps WHERE run_id IN (SELECT skill_run_id FROM turns UNION SELECT skill_run_id FROM steps))
        SELECT 1 FROM tasks t JOIN related r USING(task_id)
        WHERE (t.execution_outcome='unknown' OR t.status NOT IN ('succeeded','failed','cancelled','timed_out')
        OR EXISTS(SELECT 1 FROM task_attempts a WHERE a.task_id=t.task_id AND a.execution_stopped=0)) LIMIT 1""", (identifier,)):
        raise AccessDenied('ARCHIVE_UNSETTLED_EXECUTION')


async def records(conn, conversation_id):
    collected = []; total = 0
    for table, (key, fields) in FIELDS.items():
        where = 'conversation_id=?' if table == 'chat_turns' else 'turn_id IN (SELECT turn_id FROM chat_turns WHERE conversation_id=?)'
        # Enumerate bounded identities first; large payloads are read one by one.
        rows = await conn.execute_fetchall(f'SELECT {key} FROM {table} WHERE {where} ORDER BY rowid LIMIT 2001', (conversation_id,))
        if len(rows) > 2000:
            raise AccessDenied('ARCHIVE_BATCH_LIMIT')
        for row in rows:
            item = await one(conn, f'SELECT {key},{",".join(fields)} FROM {table} WHERE {key}=?', (row[0],))
            if await one(conn,'SELECT 1 FROM cold_payloads WHERE table_name=? AND record_id=?',(table,row[0])):
                continue
            raw = canonical({name:item[name] for name in fields}).encode()
            total += len(raw)
            if len(raw)>MAX_ROW or total>MAX_BATCH:
                raise AccessDenied('ARCHIVE_BATCH_LIMIT')
            collected.append((table, row[0], raw))
    return collected


async def preview(conn, conversation):
    await eligible(conn, conversation)
    items = await records(conn, conversation['conversation_id'])
    manifest = [{'table':table,'id':key,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)} for table,key,raw in items]
    return {'digest':digest_value(manifest),'rows':len(manifest),'original_bytes':sum(item['bytes'] for item in manifest),
            'already_archived':bool(conversation['cold_archived'])}


async def archive(conn, conversation, expected_digest):
    current = await preview(conn, conversation)
    if current['digest'] != expected_digest:
        raise Conflict('archive preview changed')
    used = (await one(conn,'SELECT coalesce(sum(length(content)),0) FROM cold_payloads'))[0]
    for table, key, raw in await records(conn, conversation['conversation_id']):
        compressed = zlib.compress(raw, level=6)
        used += len(compressed)
        if used>MAX_COLD:
            raise AccessDenied('ARCHIVE_CAPACITY')
        digest = hashlib.sha256(raw).hexdigest()
        await conn.execute('INSERT INTO cold_payloads VALUES(?,?,?,?,?,?,?)', (table,key,conversation['conversation_id'],digest,len(raw),compressed,timestamp(utc_now())))
        saved = await one(conn,'SELECT * FROM cold_payloads WHERE table_name=? AND record_id=?',(table,key))
        if canonical(unpack(saved)).encode() != raw:
            raise AccessDenied('ARCHIVE_INVALID')
        column, fields = FIELDS[table]
        await conn.execute(f'UPDATE {table} SET '+','.join(name+'=?' for name in fields)+f' WHERE {column}=?', (*fields.values(),key))
    await conn.execute('UPDATE conversations SET cold_archived=1,revision=revision+1 WHERE conversation_id=?',(conversation['conversation_id'],))
    await audit(conn,conversation['owner_id'],None,'storage.archived',utc_now())
    return current | {'archived':True,'cold_bytes':used}


async def restore(conn, conversation):
    if not conversation['cold_archived']:
        return
    # Restoring cannot silently evade the original hot-store limits.
    count=(await one(conn,'SELECT count(*) FROM conversations WHERE cold_archived=0'))[0]
    turns=(await one(conn,'SELECT count(*) FROM chat_turns t JOIN conversations c USING(conversation_id) WHERE c.cold_archived=0 OR c.conversation_id=?',(conversation['conversation_id'],)))[0]
    if count>=100 or turns>=10000:
        raise AccessDenied('ARCHIVE_RESTORE_CAPACITY')
    rows=await conn.execute_fetchall('SELECT * FROM cold_payloads WHERE conversation_id=?',(conversation['conversation_id'],))
    extra=sum(row['original_bytes'] for row in rows)
    hot=(await one(conn,"SELECT coalesce(sum(length(CAST(context_json AS BLOB))+length(CAST(response_text AS BLOB))),0) FROM chat_turns"))[0]
    hot+=(await one(conn,"SELECT coalesce(sum(length(CAST(request_body AS BLOB))+length(CAST(partial_text AS BLOB))+coalesce(length(CAST(response_json AS BLOB)),0)),0) FROM chat_exchanges"))[0]
    hot+=(await one(conn,"SELECT coalesce(sum(length(CAST(source_messages_json AS BLOB))+length(CAST(summary_text AS BLOB))),0) FROM chat_compactions"))[0]
    if hot+extra>240*1024*1024:
        raise AccessDenied('ARCHIVE_RESTORE_CAPACITY')
    for row in rows:
        table=row['table_name']; column, fields=FIELDS[table]; value=unpack(row)
        if set(value)!=set(fields):
            raise AccessDenied('ARCHIVE_INVALID')
        await conn.execute(f'UPDATE {table} SET '+','.join(name+'=?' for name in fields)+f' WHERE {column}=?',(*(value[name] for name in fields),row['record_id']))
    await conn.execute('DELETE FROM cold_payloads WHERE conversation_id=?',(conversation['conversation_id'],))
    await conn.execute('UPDATE conversations SET cold_archived=0 WHERE conversation_id=?',(conversation['conversation_id'],))
    await audit(conn,conversation['owner_id'],None,'storage.restored',utc_now())
