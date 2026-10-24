"""Bounded ordinary-history retrieval using the same consent checks as chat."""

from nexus.core.cognitive.chat_compaction import reusable_turn
from nexus.core.storage.database import one
from nexus.core.security.types import AccessDenied


async def authority_allowed(conn, authority_id, *, current_turn_id=None):
    """Derived plans/prompts cannot outlive the consent of their source turns."""
    from nexus.core.memory.automatic import reusable
    rows=await conn.execute_fetchall('''SELECT turn_id,owner_id,memory_refs_json,references_json,attachments_json,
        auto_memory_revision,auto_memory_expires_at FROM chat_turns WHERE turn_id IN (
        SELECT root_turn_id FROM plugin_authorities WHERE authority_id=? UNION
        SELECT turn_id FROM plugin_wakeups WHERE authority_id=?)''',(str(authority_id),str(authority_id)))
    for row in rows:
        permitted=await reusable(conn,row) if row['turn_id']==current_turn_id else await reusable_turn(conn,row)
        if not permitted:
            raise AccessDenied('AUTONOMY_CONTEXT_CONSENT_REQUIRED')


async def visible(conn, principal, conversation, turn_id):
    row = await one(conn, '''SELECT t.* FROM chat_turns t JOIN conversations c USING(conversation_id)
        WHERE t.turn_id=? AND t.owner_id=? AND c.project_id IS ? AND c.archived=0''',
        (turn_id,str(principal.identity_id),conversation['project_id']))
    if row is None or not await reusable_turn(conn,row):
        raise AccessDenied('PRIVATE_REFERENCE_PREVIEW_REQUIRED')
    return row


async def search(conn, principal, conversation, *, query='', conversation_id=None, limit=10):
    clauses = ' AND c.conversation_id=?' if conversation_id else ''
    args = [str(principal.identity_id),conversation['project_id']]
    if conversation_id:
        args.append(conversation_id)
    # Filter before matching: withdrawn personal context cannot reveal a hit.
    rows = await conn.execute_fetchall('''SELECT t.*,c.title FROM chat_turns t JOIN conversations c USING(conversation_id)
        WHERE t.owner_id=? AND c.project_id IS ? AND c.archived=0 AND t.status='completed'
        AND t.memory_refs_json='[]' AND t.references_json='[]' AND t.attachments_json='[]' '''+clauses+' ORDER BY t.rowid DESC LIMIT 200',tuple(args))
    result=[]
    for row in rows:
        if await reusable_turn(conn,row) and (not query or query.casefold() in (row['text']+'\n'+row['response_text']+'\n'+row['title']).casefold()):
            result.append({'conversation_id':row['conversation_id'],'title':row['title'],'turn_id':row['turn_id'],
                           'status':row['status'],'input':row['text'][:500],'response':row['response_text'][:1000],
                           'verification':'conversation_record','truncated':len(row['text'])>500 or len(row['response_text'])>1000})
        if len(result)>=limit:
            break
    return result
