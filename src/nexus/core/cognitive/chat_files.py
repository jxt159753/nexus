"""Conversation file cards and transfer sources backed by verified stored bytes."""

import hashlib
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from nexus.contracts.attachments import UploadMetadata
from nexus.contracts.file_transfer import ChatFile
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one


def import_id(identifier):
    return uuid5(NAMESPACE_URL, 'nexus-file-source:'+str(identifier))


async def source(conn, owner_id, conversation_id, kind, identifier, *, content=False):
    if kind == 'attachment':
        columns='attachment_id,owner_id,conversation_id,filename,sha256,size_bytes,preview_json'
        row = await one(conn, 'SELECT '+columns+(',content' if content else '')+' FROM chat_attachments WHERE attachment_id=? AND owner_id=? AND conversation_id=? AND deleted_at IS NULL',
                        (str(identifier),str(owner_id),str(conversation_id)))
    elif kind == 'deliverable':
        columns='d.deliverable_id,d.owner_id,d.conversation_id,d.name AS filename,d.sha256,length(d.content) AS size_bytes'
        row = await one(conn, "SELECT "+columns+(',d.content' if content else '')+" FROM plugin_deliverables d JOIN tasks t USING(task_id) WHERE d.deliverable_id=? AND d.owner_id=? AND d.conversation_id=? AND t.status='succeeded' AND t.execution_outcome='known' AND t.verification IS NOT NULL",
                        (str(identifier),str(owner_id),str(conversation_id)))
    else:
        row = None
    if row is None:
        raise AccessDenied('FILE_SOURCE_UNAVAILABLE')
    return row


async def materialize(chat, principal, conversation_id, kind, identifier):
    async with chat.database.transaction(write=False) as conn:
        await chat.conversation(conn,principal,conversation_id)
        row = await source(conn,principal.identity_id,conversation_id,kind,identifier,content=kind=='deliverable')
    if kind == 'attachment':
        return dict(row)
    raw = row['content']
    if len(raw)!=row['size_bytes'] or hashlib.sha256(raw).hexdigest()!=row['sha256']:
        raise ValueError('File source integrity mismatch')
    return await chat.attachments.upload(principal,conversation_id,
        UploadMetadata(request_id=import_id(identifier),filename=row['filename'],media_type='application/octet-stream'),raw)


async def bound_attachment(conn, owner_id, conversation_id, arguments):
    row = await source(conn,owner_id,conversation_id,arguments['source_kind'],arguments['source_id'])
    if arguments['source_kind'] == 'attachment':
        return row
    result = await one(conn,'SELECT attachment_id,conversation_id,filename,sha256,size_bytes FROM chat_attachments WHERE owner_id=? AND conversation_id=? AND request_id=? AND deleted_at IS NULL',
                       (str(owner_id),str(conversation_id),str(import_id(arguments['source_id']))))
    if result is None or result['sha256']!=row['sha256'] or result['size_bytes']!=row['size_bytes']:
        raise AccessDenied('FILE_SOURCE_UNAVAILABLE')
    return result


def card(row, kind, identifier, status='可打开'):
    conversation_id = row['conversation_id']
    base = f'/api/v1/chat/conversations/{conversation_id}/files/{kind}/{identifier}'
    download = (f'/api/v1/chat/conversations/{conversation_id}/attachments/{identifier}/content'
                if kind=='attachment' else '/api/v1/plugins/deliverables/'+str(identifier))
    return ChatFile(source_kind=kind,source_id=UUID(str(identifier)),conversation_id=UUID(str(conversation_id)),
        filename=row['filename'],size_bytes=row['size_bytes'],sha256=row['sha256'],download_url=download,
        preview_url=base+'/preview',status=status).model_dump(mode='json')


async def turn_files(conn, turn):
    files = {}
    for metadata in turn.get('attachments',[])[:4]:
        identifier=metadata['attachment_id']
        try:
            value=await source(conn,turn['owner_id'],turn['conversation_id'],'attachment',identifier)
        except AccessDenied:
            continue
        files[('attachment',identifier)]=card(value,'attachment',identifier)
    # Only task-linked, verified exports appear; a model-authored URL never creates a card.
    rows = await conn.execute_fetchall("""SELECT d.deliverable_id FROM plugin_deliverables d
        JOIN chat_work_tasks k USING(task_id) JOIN chat_work_steps s USING(step_id)
        WHERE s.turn_id=? AND d.owner_id=? ORDER BY d.rowid LIMIT 8""",(turn['turn_id'],turn['owner_id']))
    for row in rows:
        try:
            value=await source(conn,turn['owner_id'],turn['conversation_id'],'deliverable',row[0])
        except AccessDenied:
            continue
        files[('deliverable',row[0])]=card(value,'deliverable',row[0])
    rows = await conn.execute_fetchall("""SELECT t.action_json,t.status,t.execution_outcome FROM tasks t
        JOIN chat_work_tasks k USING(task_id) JOIN chat_work_steps s USING(step_id)
        WHERE s.turn_id=? AND t.owner_id=? AND json_extract(t.action_json,'$.tool')='filesystem.receive'
        ORDER BY t.rowid LIMIT 8""",(turn['turn_id'],turn['owner_id']))
    for row in rows:
        identifier=json.loads(row['action_json'])['arguments']['attachment_id']
        try:
            value=await source(conn,turn['owner_id'],turn['conversation_id'],'attachment',identifier)
        except AccessDenied:
            continue
        status='已送达设备' if row['status']=='succeeded' and row['execution_outcome']=='known' else '传输结果待核对' if row['execution_outcome']=='unknown' else '传输中' if row['status'] not in {'failed','cancelled','timed_out'} else '未送达设备'
        # The imported copy and original export share bytes; keep the delivery card.
        files={key:item for key,item in files.items() if item['sha256']!=value['sha256'] or item['filename']!=value['filename']}
        files[('attachment',identifier)]=card(value,'attachment',identifier,status)
    return list(files.values())[:8]
