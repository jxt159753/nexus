"""Versioned MCP authority. Network/process work only happens as Node tasks."""

import json
from uuid import UUID, uuid4

from nexus.contracts.extensions import canonical, digest_value
from nexus.core.security.modes import effective
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, audit, timestamp, utc_now
from nexus.core.storage.tasks import Conflict, load_task, TERMINAL
from nexus.plugins.mcp_contracts import MCPBinding, MCPConfiguration, server_value, check_call
from nexus.plugins.config import MCPServer

DISCOVERY={'mcp_register','mcp_reload','mcp_discover'}
MANAGEMENT={'mcp_list','mcp_remove'}


def public(row):
    value={key:row[key] for key in ('registration_id','node_id','server','revision','configuration_digest','catalog_digest','state','created_at')}
    value['configuration']=json.loads(row['configuration_json'])
    value['tools']=json.loads(row['catalog_json']) if row['catalog_json'] else []
    return value


async def rows(conn,owner,node):
    return [public(row) for row in await conn.execute_fetchall('SELECT * FROM mcp_registrations WHERE owner_id=? AND node_id=? ORDER BY server,revision DESC LIMIT 256',(str(owner),str(node)))]


async def model_list(conn,owner,node,server=None):
    if server:
        latest=await one(conn,'SELECT * FROM mcp_registrations WHERE owner_id=? AND node_id=? AND server=? ORDER BY revision DESC LIMIT 1',
                         (str(owner),str(node),server))
        if latest is None:
            return {'server':server,'state':'not_registered','revision':0,'latest_revision':0,'catalog_digest':None,'tools':[]}
        row=await one(conn,"SELECT * FROM mcp_registrations WHERE owner_id=? AND node_id=? AND server=? AND state='active'",
                      (str(owner),str(node),server)) or latest
        return {'server':server,'state':row['state'],'revision':row['revision'],'latest_revision':latest['revision'],
                'configuration':json.loads(row['configuration_json']),'configuration_digest':row['configuration_digest'],
                'catalog_digest':row['catalog_digest'],'tools':json.loads(row['catalog_json']) if row['catalog_json'] else []}
    # A failed/pending replacement does not deactivate the last working version.
    # Keep the summary consistent with the server-specific lookup above.
    found=await conn.execute_fetchall("""SELECT * FROM (
        SELECT server,revision,state,configuration_digest,catalog_digest,
               max(revision) OVER (PARTITION BY server) AS latest_revision,
               row_number() OVER (PARTITION BY server ORDER BY (state='active') DESC,revision DESC) AS priority
        FROM mcp_registrations WHERE owner_id=? AND node_id=?
        ) WHERE priority=1 ORDER BY server LIMIT 21""", (str(owner),str(node)))
    return {'servers':[{key:row[key] for key in ('server','revision','latest_revision','state','configuration_digest','catalog_digest')} for row in found[:20]],'truncated':len(found)>20}


async def active(conn,owner,node,server):
    row=await one(conn,"SELECT * FROM mcp_registrations WHERE owner_id=? AND node_id=? AND server=? AND state='active'",(str(owner),str(node),server))
    if row is None: raise AccessDenied('MCP_SERVER_UNAVAILABLE')
    return row


async def prepare(conn,owner,node,conversation,operation_id,name,value):
    """Must be inside the caller's operation lock and DB transaction."""
    mode=await effective(conn,conversation)
    cursor=(await one(conn,"SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
    encoded=canonical({'node_id':str(node),'conversation_id':str(conversation),'tool':name,'input':value})
    old=await one(conn,'SELECT * FROM mcp_operations WHERE operation_id=?',(str(operation_id),))
    if old:
        if old['owner_id']!=str(owner) or old['request_json']!=encoded: raise Conflict('IDEMPOTENCY_CONFLICT')
        row=await one(conn,'SELECT * FROM mcp_registrations WHERE registration_id=?',(old['registration_id'],))
        return binding(row,old['permission_fingerprint'],old['cancel_cursor']).model_copy(update={'catalog_digest':None})
    if name=='mcp_register':
        config=MCPConfiguration.model_validate_json(canonical(value))
        expected=config.expected_revision
        configuration=server_value(config.configuration)
    elif name in {'mcp_reload','mcp_discover'}:
        expected=value.get('expected_revision')
        current=await active(conn,owner,node,value['server'])
        configuration=json.loads(current['configuration_json'])
    else:
        row=await active(conn,owner,node,value['server'])
        if name=='mcp_call_tool':
            if value['catalog_digest']!=row['catalog_digest']: raise AccessDenied('MCP_CATALOG_CHANGED')
            check_call(json.loads(row['catalog_json']),value['name'],value.get('arguments',{}))
        return binding(row,mode['fingerprint'],cursor)
    latest=await one(conn,'SELECT coalesce(max(revision),0) FROM mcp_registrations WHERE owner_id=? AND node_id=? AND server=?',(str(owner),str(node),value['server']))
    if expected is None: expected=latest[0]
    if latest[0]!=expected: raise Conflict('MCP_REVISION_CONFLICT')
    if await one(conn,"SELECT 1 FROM mcp_registrations WHERE owner_id=? AND node_id=? AND server=? AND state='pending'",(str(owner),str(node),value['server'])):
        raise Conflict('MCP_DISCOVERY_PENDING')
    if (await one(conn,'SELECT count(*) FROM mcp_registrations'))[0]>=1000: raise AccessDenied('MCP_STORAGE_FULL')
    identifier=str(uuid4());now=timestamp(utc_now())
    await conn.execute("INSERT INTO mcp_registrations(registration_id,owner_id,node_id,server,revision,configuration_json,configuration_digest,state,created_at) VALUES(?,?,?,?,?,?,?,'pending',?)",
        (identifier,str(owner),str(node),value['server'],expected+1,canonical(configuration),digest_value(configuration),now))
    await conn.execute('INSERT INTO mcp_operations(operation_id,owner_id,conversation_id,registration_id,request_json,permission_fingerprint,cancel_cursor,created_at) VALUES(?,?,?,?,?,?,?,?)',
        (str(operation_id),str(owner),str(conversation),identifier,encoded,mode['fingerprint'],cursor,now))
    await audit(conn,str(owner),None,'mcp.configuration_staged',utc_now())
    return binding(await one(conn,'SELECT * FROM mcp_registrations WHERE registration_id=?',(identifier,)),mode['fingerprint'],cursor)


def binding(row,fingerprint,cursor):
    return MCPBinding(registration_id=UUID(row['registration_id']),server=row['server'],revision=row['revision'],
        configuration=MCPServer.model_validate_json(row['configuration_json']),configuration_digest=row['configuration_digest'],
        catalog_digest=row['catalog_digest'],permission_fingerprint=fingerprint,cancel_cursor=cursor)


async def authorize(conn,task):
    args=task.action.arguments
    if args.plugin_id!='builtin.mcp':
        if args.mcp_binding is not None: raise AccessDenied('MCP_BINDING_INVALID')
        return
    bound=args.mcp_binding
    if bound is None:
        if args.tool in DISCOVERY|{'mcp_call_tool'}: raise AccessDenied('MCP_REGISTRATION_REQUIRED')
        if await one(conn,'SELECT 1 FROM mcp_registrations WHERE owner_id=? AND node_id=? AND server=?',(str(task.owner_id),str(task.node_id),args.input.get('server'))):
            raise AccessDenied('MCP_BINDING_REQUIRED')
        return  # Existing static resource configurations remain compatible.
    row=await one(conn,'SELECT * FROM mcp_registrations WHERE registration_id=? AND owner_id=? AND node_id=?',(str(bound.registration_id),str(task.owner_id),str(task.node_id)))
    if row is None or bound.server!=args.input.get('server'): raise AccessDenied('MCP_BINDING_INVALID')
    conversation=await one(conn,'SELECT owner_id,archived FROM conversations WHERE conversation_id=?',(str(args.context_id),))
    if conversation is None or conversation['owner_id']!=str(task.owner_id) or conversation['archived']: raise AccessDenied('WORK_STOPPED')
    mode=await effective(conn,args.context_id)
    cursor=(await one(conn,"SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
    historical=(task.status=='succeeded' and task.execution_outcome=='known' and task.verification=='node_report_schema_checked')
    expected=binding(row,mode['fingerprint'],cursor)
    if historical and args.tool in DISCOVERY: expected=expected.model_copy(update={'catalog_digest':None})
    if expected!=bound: raise AccessDenied('MCP_BINDING_CHANGED')
    if args.tool in DISCOVERY:
        operation=await one(conn,'SELECT * FROM mcp_operations WHERE operation_id=?',(str(args.invocation_id),))
        if (row['state'] not in ({'active','inactive'} if historical else {'pending'}) or operation is None or operation['task_id']!=str(task.task_id) or operation['status']!=('succeeded' if historical else 'pending')
                or operation['registration_id']!=row['registration_id'] or operation['conversation_id']!=str(args.context_id)
                or operation['owner_id']!=str(task.owner_id)):
            raise AccessDenied('MCP_BINDING_CHANGED')
        expected=json.loads(operation['request_json'])
        if expected['tool']!=args.tool or expected['input']!=args.input: raise AccessDenied('MCP_BINDING_CHANGED')
    elif row['state'] not in ({'active','inactive'} if historical else {'active'}): raise AccessDenied('MCP_SERVER_UNAVAILABLE')


async def discovery_succeeded(conn,task):
    """A successful process result alone does not activate a configuration."""
    if task.action.arguments.tool not in DISCOVERY: return True
    await settle(conn,task)
    row=await one(conn,'SELECT status FROM mcp_operations WHERE operation_id=? AND task_id=?',
                  (str(task.action.arguments.invocation_id),str(task.task_id)))
    return row is not None and row[0]=='succeeded'


async def settle(conn,task):
    args=task.action.arguments
    if args.plugin_id!='builtin.mcp' or args.tool not in DISCOVERY or args.mcp_binding is None: return
    op=await one(conn,'SELECT * FROM mcp_operations WHERE operation_id=?',(str(args.invocation_id),))
    if op is None or op['status']!='pending' or op['task_id']!=str(task.task_id): return
    row=await one(conn,'SELECT * FROM mcp_registrations WHERE registration_id=?',(op['registration_id'],))
    status='failed';result={'reason':'discovery_not_verified'}
    if task.status=='succeeded' and task.execution_outcome=='known' and task.verification=='node_report_schema_checked' and not task.cancel_requested:
        try:
            mode=await effective(conn,op['conversation_id'])
            conversation=await one(conn,'SELECT archived FROM conversations WHERE conversation_id=?',(op['conversation_id'],))
            owner=await one(conn,"SELECT revoked_at FROM identities WHERE identity_id=? AND role='owner'",(op['owner_id'],))
            cursor=(await one(conn,"SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
            if mode['fingerprint']!=op['permission_fingerprint'] or cursor!=op['cancel_cursor'] or conversation[0] or owner is None or owner[0]: raise ValueError('authority changed')
            if row['state']!='pending': raise ValueError('registration no longer pending')
            data=task.result.data
            if str(args.mcp_binding.registration_id)!=row['registration_id'] or data.get('verification')!='mcp_tools_discovered':
                raise ValueError('MCP task evidence mismatch')
            # Node already normalized these tools. Re-normalizing would erase
            # disabled schema metadata, so compare the canonical digest directly.
            from nexus.plugins.schema import bounded
            bounded(data['tools'],20000)
            if digest_value(data['tools'])!=data['catalog_digest']: raise ValueError('MCP catalog evidence mismatch')
            if len(data['tools'])>64 or len({item['name'] for item in data['tools']})!=len(data['tools']): raise ValueError('invalid discovered tools')
            # Revalidate every callable schema before accepting Node evidence.
            from nexus.plugins.mcp_contracts import tool_schema
            for item in data['tools']:
                if item['supported']:
                    tool_schema(item['inputSchema'])
                    if 'outputSchema' in item: tool_schema(item['outputSchema'])
            await conn.execute("UPDATE mcp_registrations SET state='inactive' WHERE owner_id=? AND node_id=? AND server=? AND state='active'",(row['owner_id'],row['node_id'],row['server']))
            await conn.execute("UPDATE mcp_registrations SET state='active',catalog_json=?,catalog_digest=? WHERE registration_id=?",(canonical(data['tools']),data['catalog_digest'],row['registration_id']))
            status='succeeded';result={'registration_id':row['registration_id'],'revision':row['revision'],'catalog_digest':data['catalog_digest']}
        except (ValueError,KeyError,TypeError): result={'reason':'discovery_evidence_or_authority_changed'}
    if status!='succeeded': await conn.execute("UPDATE mcp_registrations SET state='failed' WHERE registration_id=? AND state='pending'",(row['registration_id'],))
    await conn.execute('UPDATE mcp_operations SET status=?,result_json=? WHERE operation_id=?',(status,canonical(result),op['operation_id']))
    await audit(conn,op['owner_id'],str(task.task_id),'mcp.discovery_'+status,utc_now())


async def poll(service):
    if not service.config.enabled: return
    async with service.chat.harness.operation,service.database.transaction() as conn:
        operations=await conn.execute_fetchall("SELECT task_id FROM mcp_operations WHERE status='pending' AND task_id IS NOT NULL LIMIT 100")
        for operation in operations:
            task=await load_task(conn,UUID(operation['task_id']))
            if task.status in TERMINAL: await settle(conn,task)


async def remove(conn,owner,node,server,revision,operation_id,conversation):
    value=canonical({'action':'remove','server':server,'revision':revision,'node':str(node),'conversation':str(conversation)})
    prior=await one(conn,'SELECT * FROM mcp_operations WHERE operation_id=?',(str(operation_id),))
    if prior:
        if prior['owner_id']!=str(owner) or prior['request_json']!=value: raise Conflict('IDEMPOTENCY_CONFLICT')
        return json.loads(prior['result_json'])
    latest=await one(conn,'SELECT * FROM mcp_registrations WHERE owner_id=? AND node_id=? AND server=? ORDER BY revision DESC LIMIT 1',(str(owner),str(node),server))
    if latest is None: raise LookupError('MCP server not found')
    if latest['revision']!=revision: raise Conflict('MCP_REVISION_CONFLICT')
    await conn.execute("UPDATE mcp_registrations SET state='removed' WHERE owner_id=? AND node_id=? AND server=? AND state IN ('active','pending')",(str(owner),str(node),server))
    mode=await effective(conn,conversation);result={'server':server,'removed':True}
    cursor=(await one(conn,"SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
    if (await one(conn,'SELECT count(*) FROM mcp_operations'))[0]>=10000: raise AccessDenied('MCP_STORAGE_FULL')
    await conn.execute("INSERT INTO mcp_operations(operation_id,owner_id,conversation_id,registration_id,request_json,permission_fingerprint,cancel_cursor,status,result_json,created_at) VALUES(?,?,?,?,?,?,?,'succeeded',?,?)",
        (str(operation_id),str(owner),str(conversation),latest['registration_id'],value,mode['fingerprint'],cursor,canonical(result),timestamp(utc_now())))
    tasks=await conn.execute_fetchall("SELECT task_id FROM tasks WHERE owner_id=? AND node_id=? AND status NOT IN ('succeeded','failed','cancelled','timed_out') AND json_extract(action_json,'$.arguments.plugin_id')='builtin.mcp' AND json_extract(action_json,'$.arguments.input.server')=?",(str(owner),str(node),server))
    # Caller persists cancellation requests in this same transaction.
    await audit(conn,str(owner),None,'mcp.removed',utc_now())
    return {**result,'cancel_task_ids':[item[0] for item in tasks]}
