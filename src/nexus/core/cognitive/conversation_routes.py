"""Owner-scoped discovery over original chat and execution records, without a second memory store."""

import json
import re
from datetime import datetime, timedelta, timezone

from nexus.contracts.extensions import canonical
from nexus.core.memory import automatic
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp
from nexus.core.storage.archive import hydrate

LOCAL = timezone(timedelta(hours=8))
ROUTE = 'conversation_route'
PUBLIC = "t.memory_refs_json='[]' AND t.references_json='[]' AND t.attachments_json='[]'"
FIELDS = '''t.rowid AS cursor,t.turn_id,t.conversation_id,t.owner_id,t.text,t.response_text,t.status,
    t.work_status,t.recovery_required,t.created_at,t.auto_memory_revision,t.auto_memory_expires_at,
    t.memory_refs_json,t.references_json,t.attachments_json,c.title,c.project_id,c.archived,
    coalesce(p.name,'') AS project_name'''


def window(arguments, now):
    today = now.astimezone(LOCAL).date()
    period = arguments['period']
    start, end = arguments['date_from'], arguments['date_until']
    if period != 'all':
        day = today - timedelta(days=period == 'yesterday')
        start, end = day.isoformat(), (day + timedelta(days=1)).isoformat()
    utc = lambda value: timestamp(datetime.fromisoformat(value).replace(tzinfo=LOCAL).astimezone(timezone.utc)) if value else None
    return utc(start), utc(end)


def link(row):
    return {key: row[key] for key in ('conversation_id', 'title', 'project_name', 'turn_id', 'created_at')} | {'archived': bool(row['archived'])}


async def bind_memory(conn, target_id, rows):
    """A retrieved answer cannot outlive the automatic-memory consent of its source."""
    revisions = {row['auto_memory_revision'] for row in rows if row['auto_memory_revision']}
    if not revisions:
        return
    current = await one(conn, 'SELECT auto_memory_revision,auto_memory_expires_at FROM chat_turns WHERE turn_id=?', (target_id,))
    if current['auto_memory_revision']:
        revisions.add(current['auto_memory_revision'])
    if len(revisions) != 1:
        raise AccessDenied('MEMORY_UNAVAILABLE')
    expiries = [row['auto_memory_expires_at'] for row in [current, *rows] if row['auto_memory_expires_at']]
    await conn.execute('UPDATE chat_turns SET auto_memory_revision=?,auto_memory_expires_at=? WHERE turn_id=?',
                       (revisions.pop(), min(expiries) if expiries else None, target_id))


async def task_evidence(conn, owner_id, row):
    from nexus.core.cognitive.work_queries import TASK_IDS
    params = (row['conversation_id'], row['turn_id'], row['turn_id']) * 4
    tasks = await conn.execute_fetchall('''SELECT task_id,status,execution_outcome,verification,
        EXISTS(SELECT 1 FROM task_attempts a WHERE a.task_id=t.task_id AND a.execution_stopped=0) AS unsettled
        FROM tasks t WHERE t.owner_id=? AND t.task_id IN (''' + TASK_IDS + ') ORDER BY t.rowid DESC LIMIT 21',
        (owner_id, *params))
    return {'tasks': [dict(item) for item in tasks[:20]], 'tasks_truncated': len(tasks) > 20}


async def projects(conn, owner_id, name):
    rows = await conn.execute_fetchall('SELECT project_id,name FROM projects WHERE owner_id=? ORDER BY rowid DESC', (owner_id,))
    exact = [dict(row) for row in rows if name.casefold() in {row['name'].casefold(), row['project_id']}]
    return exact or [dict(row) for row in rows if name.casefold() in row['name'].casefold()]


async def project_snapshot(conn, owner_id, project, now):
    identifier = project['project_id']
    counts = await conn.execute_fetchall('''SELECT status,execution_outcome,verification,count(*) AS count
        FROM tasks WHERE owner_id=? AND project_id=? GROUP BY status,execution_outcome,verification''', (owner_id, identifier))
    todos = await conn.execute_fetchall('''SELECT todo_id,text,status,evidence_task_id,updated_at
        FROM project_todos WHERE owner_id=? AND project_id=? ORDER BY rowid DESC LIMIT 21''', (owner_id, identifier))
    sessions = await conn.execute_fetchall('''SELECT conversation_id,title,created_at,archived FROM conversations
        WHERE owner_id=? AND project_id=? ORDER BY rowid DESC LIMIT 11''', (owner_id, identifier))
    return {'project': project, 'observed_at': timestamp(now), 'task_counts': [dict(row) for row in counts],
            'todos': [dict(row) for row in todos[:20]], 'todos_truncated': len(todos) > 20,
            'conversations': [dict(row) | {'project_name': project['name'], 'archived': bool(row['archived'])} for row in sessions[:10]],
            'conversations_truncated': len(sessions) > 10,
            'notice': '任务成功只证明该任务的验证级别；待办与聊天自述不能证明整个项目完成。未登记的工作不在统计内。'}


async def query(conn, step, grant, arguments, now):
    owner_id = grant['owner_id']
    action = arguments['action']
    project_id = None
    if arguments['project']:
        matches = await projects(conn, owner_id, arguments['project'].strip())
        if len(matches) != 1:
            return {'projects': matches[:20], 'truncated': len(matches) > 20,
                    'needs_selection': len(matches) > 1, 'notice': '请从项目候选中确定名称或编号。' if matches else '没有找到匹配的项目记录。'}
        project_id = matches[0]['project_id']
        if action == 'project':
            return await project_snapshot(conn, owner_id, matches[0], now)
    if action == 'open':
        row = await one(conn, '''SELECT c.*,coalesce(p.name,'') AS project_name,NULL AS turn_id
            FROM conversations c LEFT JOIN projects p USING(project_id) WHERE c.owner_id=? AND c.conversation_id=?''',
            (owner_id, arguments['conversation_id']))
        if row is None:
            raise LookupError('找不到这个会话')
        # Navigation is a local UI request, never an instruction to another agent.
        text = (await one(conn, 'SELECT text FROM chat_turns WHERE turn_id=?', (step['turn_id'],)))[0]
        requested = bool(re.search(r'打开|切换|切到|跳转|进入|带我去|切过去|\b(open|switch|navigate)\b', text, re.I))
        requested = requested and not re.search(r'不要.{0,6}(打开|切换|跳转)|别.{0,4}(打开|切换)|do not|don.t', text, re.I)
        return {'conversations': [link(row)], 'navigation': {'conversation_id': row['conversation_id']} if requested else None,
                'notice': '已生成打开会话入口；由发起请求的客户端切换，不发送消息、不恢复归档或继续旧任务。'}

    start, end = window(arguments, now)
    clauses = ['t.owner_id=?', 'c.owner_id=?', 't.turn_id!=?', PUBLIC]
    values = [owner_id, owner_id, step['turn_id']]
    # Routed answers are derivatives. Search their original sources instead of recursively quoting them.
    clauses.append("NOT EXISTS(SELECT 1 FROM chat_work_steps w WHERE w.turn_id=t.turn_id AND json_extract(w.proposal_json,'$.intent')='conversation_route')")
    if not arguments['include_archived']:
        clauses.append('c.archived=0')
    for clause, value in [('c.project_id=?', project_id), ('t.conversation_id=?', arguments['conversation_id']),
                          ('t.created_at>=?', start), ('t.created_at<?', end), ('t.rowid<?', arguments['before'])]:
        if value is not None:
            clauses.append(clause); values.append(value)
    # Match before paging so old hot records remain discoverable. Cold records are checked after verified hydration.
    terms = arguments['query'].casefold().split()
    for term in terms:
        clauses.append("(c.cold_archived=1 OR instr(lower(t.text||char(10)||t.response_text||char(10)||c.title),?)>0)")
        values.append(term)
    rows = await conn.execute_fetchall('SELECT ' + FIELDS + ''' FROM chat_turns t JOIN conversations c USING(conversation_id)
        LEFT JOIN projects p ON p.project_id=c.project_id WHERE ''' + ' AND '.join(clauses) + ' ORDER BY t.rowid DESC LIMIT 201', tuple(values))
    selected, records, links, used = [], [], [], 0
    next_before = None
    for index, raw in enumerate(rows[:200]):
        row = await hydrate(conn, 'chat_turns', raw)
        if not await automatic.reusable(conn, row):
            continue
        if any(term not in (row['text']+'\n'+row['response_text']+'\n'+row['title']).casefold() for term in terms):
            continue
        # Partial/active content is not exported as a finished answer; status is still discoverable.
        finished = row['status'] in {'completed','failed','stopped','interrupted','incomplete','expired'} and not row['work_status'] and not row['recovery_required']
        cap = 1600 if action == 'read' else 600
        item = link(row) | {'status': row['status'], 'work_status': row['work_status'], 'verification': 'conversation_record',
                            'input': row['text'][:500] if finished else '',
                            'response': row['response_text'][:cap] if finished else '',
                            'content_available': bool(finished), 'truncated': not finished or len(row['text']) > 500 or len(row['response_text']) > cap}
        item.update(await task_evidence(conn, owner_id, row))
        size = len(canonical(item).encode())
        if records and used + size > 14000:
            next_before = row['cursor'] + 1
            break
        records.append(item); selected.append(row); links.append(link(row)); used += size
        if len(records) >= arguments['limit']:
            next_before = row['cursor'] if index + 1 < len(rows) else None
            break
    else:
        if len(rows) > 200:
            next_before = rows[199]['cursor']
    await bind_memory(conn, step['turn_id'], selected)
    return {'observed_at': timestamp(now), 'timezone': 'Asia/Shanghai', 'start_inclusive': start, 'end_exclusive': end,
            'turns': records, 'conversations': links, 'next_before': next_before, 'truncated': next_before is not None,
            'notice': '只检索本人记录；附件、显式私密引用及失效自动记忆不外发。聊天完成不等于功能完成。命中后用 read 查该会话最新结果；无结果不证明没有做过。'}


async def presentation(conn, turn):
    """Create clickable targets from successful Core results, never model-authored URLs."""
    links, seen, navigation = [], set(), None
    for step in turn['work_steps']:
        if step.get('proposal', {}).get('intent') != ROUTE or step['status'] != 'succeeded':
            continue
        data = (step.get('result') or {}).get('data') or {}
        for item in data.get('conversations', []):
            identifier = item['conversation_id']
            if identifier in seen or len(links) >= 10:
                continue
            owned = await one(conn, 'SELECT title,archived FROM conversations WHERE conversation_id=? AND owner_id=?', (identifier, turn['owner_id']))
            if owned is None:
                continue
            seen.add(identifier)
            links.append({key: item[key] for key in ('conversation_id','project_name','turn_id','created_at') if key in item}
                         | {'title': owned['title'], 'archived': bool(owned['archived'])})
        target = data.get('navigation')
        if target and target.get('conversation_id') in seen:
            navigation = {'step_id': step['step_id'], 'conversation_id': target['conversation_id']}
    return links, navigation
