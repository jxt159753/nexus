"""Verified bounded artifact bytes, distinct from model-generated links."""

import base64
import hashlib
from uuid import uuid5,NAMESPACE_URL
from nexus.core.storage.database import one,timestamp,utc_now


async def store(conn,principal,conversation,task,value):
    files=value.get('files',[])
    if not isinstance(files,list) or len(files)>4: raise ValueError('invalid deliverable list')
    used=await one(conn,'SELECT count(*),coalesce(sum(length(content)),0) FROM plugin_deliverables')
    if used[0]+len(files)>1000: raise ValueError('deliverable capacity exceeded')
    result=[];total=used[1]
    for item in files:
        if 'deliverable_id' in item:
            row=await one(conn,'SELECT * FROM plugin_deliverables WHERE deliverable_id=? AND task_id=? AND owner_id=?',(item['deliverable_id'],str(task.task_id),str(principal.identity_id)))
            if row is None or row['sha256']!=item['sha256'] or len(row['content'])!=item['size_bytes']: raise ValueError('artifact transfer evidence mismatch')
            result.append({key:item[key] for key in ('deliverable_id','name','description','size_bytes','sha256','download_url')})
            continue
        content=base64.b64decode(item['content_base64'],validate=True)
        if len(content)!=item['size_bytes'] or hashlib.sha256(content).hexdigest()!=item['sha256']: raise ValueError('deliverable digest mismatch')
        total+=len(content)
        if total>16*1024*1024: raise ValueError('deliverable byte capacity exceeded')
        identifier=str(uuid5(NAMESPACE_URL,f'{task.task_id}:{len(result)}'))
        await conn.execute('INSERT OR IGNORE INTO plugin_deliverables VALUES(?,?,?,?,?,?,?,?,?)',
            (identifier,str(principal.identity_id),conversation['conversation_id'],str(task.task_id),item['name'],item['description'],item['sha256'],content,timestamp(utc_now())))
        result.append({'deliverable_id':identifier,'name':item['name'],'description':item['description'],'size_bytes':len(content),'sha256':item['sha256'],'download_url':'/api/v1/plugins/deliverables/'+identifier})
    return {'files':result,'verification':'file_bytes_digest_checked'}
