"""Immutable plugin releases, owner decisions and the existing task executor."""

import json
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from nexus.contracts.extensions import canonical
from nexus.plugins.catalog import RELEASES
from nexus.plugins.contracts import PluginRelease, PluginNodeArguments
from nexus.plugins.schema import checked
from nexus.core.security.types import AccessDenied
from nexus.core.security.service import assert_owner
from nexus.core.security import work as permissions
from nexus.core.security.modes import effective
from nexus.core.storage.database import one, audit, timestamp, utc_now
from nexus.core.storage.tasks import Conflict, REQUEST, TERMINAL, load_task


def public_version(row,detail=False):
    value={key:row[key] for key in ('version_id','plugin_id','version','digest','origin','builtin','test_status','approved','enabled','created_at')}
    release=PluginRelease.model_validate_json(row['release_json'])
    value.update(name=release.manifest.name,description=release.manifest.description,runtime=release.manifest.runtime,
                 tools=[tool.name for tool in release.manifest.tools])
    if detail:
        value.update(release=release.model_dump(mode='json'),test=json.loads(row['test_json']))
    return value


async def authorize_task(conn,task):
    args=task.action.arguments
    row=await one(conn,"SELECT * FROM plugin_versions WHERE owner_id=? AND plugin_id=? AND digest=? AND enabled=1 AND approved=1 AND (builtin=1 OR test_status='passed')",
                  (str(task.owner_id),args.plugin_id,args.release_digest))
    if row is None: raise AccessDenied('PLUGIN_VERSION_UNAVAILABLE')
    release=PluginRelease.model_validate_json(row['release_json'])
    tool=next((tool for tool in release.manifest.tools if tool.name==args.tool),None)
    if tool is None or release.manifest.runtime=='core': raise AccessDenied('PLUGIN_TOOL_UNAVAILABLE')
    plan=await one(conn,'SELECT plan_mode FROM conversations WHERE conversation_id=?',(str(args.context_id),))
    if plan is None or plan[0] and (tool.effect!='read' or not row['builtin']): raise AccessDenied('PLAN_MODE_READ_ONLY')
    checked(tool.input_schema,args.input)
    from nexus.core.plugins.mcp import authorize as authorize_mcp
    await authorize_mcp(conn,task)
    if args.plugin_id.startswith('user.') and (args.release is None or args.release.digest()!=row['digest']):
        raise AccessDenied('PLUGIN_RELEASE_CHANGED')
    for plugin_id,digest in args.capability_digests.items():
        dependency=await one(conn,"SELECT 1 FROM plugin_versions WHERE owner_id=? AND plugin_id=? AND digest=? AND enabled=1 AND approved=1 AND (builtin=1 OR test_status='passed')",(str(task.owner_id),plugin_id,digest))
        if dependency is None: raise AccessDenied('PLUGIN_DEPENDENCY_CHANGED')


class PluginService:
    def __init__(self,chat):
        self.chat=chat
        self.database=chat.database
        self.config=chat.harness.link.config.plugins
        self.tester=None
        from nexus.core.plugins.health import HealthService
        self.health=HealthService(self)

    def require_enabled(self):
        if not self.config.enabled: raise AccessDenied('PLUGINS_DISABLED')

    async def seed(self):
        if not self.config.enabled: return
        async with self.database.transaction() as conn:
            for release in RELEASES.values():
                digest=release.digest()
                existing=await one(conn,'SELECT version_id FROM plugin_versions WHERE owner_id=? AND plugin_id=? AND digest=?',
                                   (str(self.chat.harness.link.config.owner_id),release.manifest.plugin_id,digest))
                if existing: continue
                # Shipped releases are part of the explicitly deployed application.
                await conn.execute('UPDATE plugin_versions SET enabled=0 WHERE owner_id=? AND plugin_id=?',
                                   (str(self.chat.harness.link.config.owner_id),release.manifest.plugin_id))
                await conn.execute("INSERT INTO plugin_versions(version_id,owner_id,plugin_id,version,digest,release_json,origin,builtin,test_status,approved,enabled,created_at) VALUES(?,?,?,?,?,?,?,1,'bundled',1,1,?)",
                    (str(uuid4()),str(self.chat.harness.link.config.owner_id),release.manifest.plugin_id,release.manifest.version,digest,release.model_dump_json(),'nexus-bundled',timestamp(utc_now())))

    async def summaries(self,conn,owner_id):
        if not self.config.enabled: return []
        rows=await conn.execute_fetchall('SELECT * FROM plugin_versions WHERE owner_id=? AND enabled=1 ORDER BY plugin_id LIMIT 64',(str(owner_id),))
        return [public_version(row) for row in rows]

    async def execution_status(self, conn, owner_id, node_id, release):
        """Observed executor and authority, separate from registry membership."""
        connection = self.chat.harness.link.connections.get(node_id)
        configured = [item.name for item in release.manifest.tools]
        status = 'available'
        if release.manifest.runtime != 'core':
            configured = connection.hello.plugin_tools.get(release.manifest.plugin_id, []) if connection else []
            if release.manifest.runtime == 'python' and connection and connection.hello.plugin_tools.get('user.python'):
                configured = [item.name for item in release.manifest.tools]
            scope = self.chat.harness.scope(node_id)
            if connection is None:
                status = 'node_offline'
            elif 'plugin.call@1' not in scope.tools & scope.local_tools or 'workspace' not in scope.roots & scope.local_roots:
                status = 'executor_not_enabled'
            elif scope.plugin_digest is None:
                status = 'policy_digest_mismatch'
            elif not configured:
                status = 'unconfigured'
            else:
                for identity in (str(owner_id), str(node_id)):
                    if not await one(conn, "SELECT 1 FROM grants WHERE identity_id=? AND tool='plugin.call' AND tool_version=1 AND root_id='workspace'", (identity,)):
                        status = 'permission_missing'
        from nexus.core.cognitive.node_routing import names
        labels=await names(conn,self.chat.harness.link.config)
        return {'node_id':str(node_id),'node_name':labels.get(str(node_id),str(node_id)),
                'location':release.manifest.runtime, 'configured_tools':configured,
                'node_connected':connection is not None, 'status':status}

    async def resolve(self,conn,owner_id,plugin_id,digest=None):
        self.require_enabled()
        row=await one(conn,"SELECT * FROM plugin_versions WHERE owner_id=? AND plugin_id=? AND enabled=1 AND approved=1 AND (builtin=1 OR test_status='passed')",(str(owner_id),plugin_id))
        if row is None or digest is not None and digest!=row['digest']: raise AccessDenied('PLUGIN_VERSION_UNAVAILABLE')
        return row,PluginRelease.model_validate_json(row['release_json'])

    async def install(self,principal,conversation_id,request_id,release,origin='owner-upload',*,work_step_id=None):
        self.require_enabled()
        if not release.manifest.plugin_id.startswith('user.') or release.manifest.runtime!='python':
            raise AccessDenied('PLUGIN_NAMESPACE_RESERVED')
        encoded=canonical(release.model_dump(mode='json'))
        async with self.chat.harness.operation,self.database.transaction() as conn:
            await self.chat.conversation(conn,principal,conversation_id)
            if work_step_id is not None:
                grant_node = await one(conn, 'SELECT g.node_id FROM chat_work_steps s JOIN chat_turns t USING(turn_id) JOIN chat_work_grants g ON g.grant_id=COALESCE(s.routed_grant_id,t.work_grant_id) WHERE s.step_id=?', (str(work_step_id),))
                if grant_node is None:
                    raise AccessDenied('WORK_STOPPED')
                fresh, grant = await permissions.creation_step(conn, work_step_id, utc_now(), scope=self.chat.harness.scope(UUID(grant_node[0])))
                if (grant['owner_id']!=str(principal.identity_id) or grant['conversation_id']!=str(conversation_id)
                        or json.loads(fresh['proposal_json'])!={'intent':'plugin_install','arguments':{'release':release.model_dump(mode='json')}}):
                    raise AccessDenied('POLICY_DENIED')
                await permissions.operation(conn, work_step_id, 'plugin-install:'+str(work_step_id), utc_now())
            old=await one(conn,'SELECT * FROM plugin_install_requests WHERE request_id=?',(str(request_id),))
            if old:
                if old['owner_id']!=str(principal.identity_id) or old['conversation_id']!=str(conversation_id) or old['request_json']!=encoded:
                    raise Conflict('IDEMPOTENCY_CONFLICT')
                return public_version(await one(conn,'SELECT * FROM plugin_versions WHERE version_id=?',(old['version_id'],)),True)
            row=await one(conn,'SELECT * FROM plugin_versions WHERE owner_id=? AND plugin_id=? AND digest=?',(str(principal.identity_id),release.manifest.plugin_id,release.digest()))
            if row is None:
                used=await one(conn,'SELECT count(*),coalesce(sum(length(CAST(release_json AS BLOB))),0) FROM plugin_versions')
                if used[0]>=self.config.max_versions or used[1]+len(encoded.encode())>self.config.max_total_bytes:
                    raise AccessDenied('PLUGIN_STORAGE_FULL')
                if await one(conn,'SELECT 1 FROM plugin_versions WHERE owner_id=? AND plugin_id=? AND version=?',(str(principal.identity_id),release.manifest.plugin_id,release.manifest.version)):
                    raise Conflict('PLUGIN_VERSION_IMMUTABLE')
                identifier=str(uuid4())
                await conn.execute("INSERT INTO plugin_versions(version_id,owner_id,plugin_id,version,digest,release_json,origin,builtin,test_status,created_at) VALUES(?,?,?,?,?,?,?,0,'pending',?)",
                    (identifier,str(principal.identity_id),release.manifest.plugin_id,release.manifest.version,release.digest(),encoded,origin[:200],timestamp(utc_now())))
                row=await one(conn,'SELECT * FROM plugin_versions WHERE version_id=?',(identifier,))
            await conn.execute('INSERT INTO plugin_install_requests VALUES(?,?,?,?,?,?)',(str(request_id),str(principal.identity_id),str(conversation_id),encoded,row['version_id'],timestamp(utc_now())))
            await audit(conn,str(principal.identity_id),None,'plugin.candidate_created',utc_now())
            return public_version(row,True)

    async def decide(self,conn,principal,row,conversation_id,action,*,automatic=False):
        await assert_owner(conn,principal,utc_now())
        mode=await effective(conn,conversation_id)
        if row['owner_id']!=str(principal.identity_id): raise AccessDenied()
        if automatic and mode['effective_mode']!='full': raise AccessDenied('PLUGIN_APPROVAL_REQUIRED')
        if action in {'approve','enable'}:
            if not row['builtin']:
                from nexus.core.plugins.business import require_business
                await require_business(conn,row)
                if row['test_status']!='passed': raise AccessDenied('PLUGIN_NOT_TESTED')
                evidence=json.loads(row['test_json'])
                environments=[self.tester.tools.extensions.sandbox_environment(node) for node in self.chat.harness.link.config.sandbox_nodes]
                if evidence.get('release_digest')!=row['digest'] or evidence.get('environment') not in environments:
                    raise AccessDenied('PLUGIN_TEST_ENVIRONMENT_CHANGED')
        if action in {'approve','enable'}:
            await conn.execute('UPDATE plugin_versions SET approved=1 WHERE version_id=?',(row['version_id'],))
        if action=='enable':
            if not row['builtin']:
                from nexus.core.plugins.business import start_rollout
                baseline=await one(conn,'SELECT * FROM plugin_versions WHERE owner_id=? AND plugin_id=? AND enabled=1',(row['owner_id'],row['plugin_id']))
                await start_rollout(conn,row,baseline,conversation_id,mode)
            await conn.execute('UPDATE plugin_versions SET enabled=0 WHERE owner_id=? AND plugin_id=?',(str(principal.identity_id),row['plugin_id']))
            await conn.execute('UPDATE plugin_versions SET enabled=1 WHERE version_id=?',(row['version_id'],))
        if action=='disable': await conn.execute('UPDATE plugin_versions SET enabled=0 WHERE version_id=?',(row['version_id'],))
        await conn.execute('UPDATE plugin_versions SET decision_revision=decision_revision+1 WHERE version_id=?',(row['version_id'],))
        await conn.execute('INSERT INTO plugin_decisions VALUES(?,?,?,?,?,?,?,?,?)',(str(uuid4()),str(principal.identity_id),row['version_id'],row['digest'],action,mode['effective_mode'],str(conversation_id),mode['fingerprint'],timestamp(utc_now())))
        await audit(conn,str(principal.identity_id),None,'plugin.'+action,utc_now())
        return public_version(await one(conn,'SELECT * FROM plugin_versions WHERE version_id=?',(row['version_id'],)))

    async def permission(self,conn,step,grant,plugin_id,tool):
        if plugin_id=='builtin.interaction': return True
        if plugin_id=='builtin.files' and grant['auto_files']: return True
        if grant['permission_mode']=='full' or step['permission_decision']=='approve': return True
        needs=tool.effect in {'execute','manage','destructive','network'} or plugin_id=='builtin.session' or plugin_id.startswith('user.')
        if not needs: return True
        # A normal-mode authorization covers one plugin within this task; version
        # management always requires the exact current action to be approved.
        if tool.effect!='manage' and plugin_id!='builtin.mcp':
            prior=await conn.execute_fetchall("SELECT s.proposal_json FROM chat_work_steps s JOIN chat_turns t USING(turn_id) WHERE s.turn_id=? AND s.permission_decision='approve' AND COALESCE(s.routed_grant_id,t.work_grant_id)=?",(step['turn_id'],grant['grant_id']))
            if any(json.loads(row[0]).get('arguments',{}).get('plugin_id')==plugin_id for row in prior): return True
        scope='当前具体操作' if plugin_id=='builtin.mcp' else '本任务内该插件'
        text=f"本任务需要使用插件 {plugin_id} 的 {tool.name}。宿主操作按当前 Windows 用户权限运行。回复“同意”授权{scope}，或回复“不同意”停止。"
        await conn.execute("UPDATE chat_work_steps SET status='permission',request_text=? WHERE step_id=?",(text,step['step_id']))
        await conn.execute("UPDATE chat_turns SET work_status='permission',revision=revision+1 WHERE turn_id=?",(step['turn_id'],))
        return False

    async def work_step(self,step,principal,node_id,proposal):
        args=proposal['arguments']; intent=proposal['intent']
        if intent=='plugin_install':
            await self.install_step(step,principal,args)
            return
        async with self.chat.harness.operation,self.database.transaction() as conn:
            fresh,grant=await permissions.allowed_step(conn,step['step_id'],utc_now(),scope=self.chat.harness.scope(node_id))
            await self.chat.owner(conn,principal)
            turn=await one(conn,'SELECT * FROM chat_turns WHERE turn_id=?',(step['turn_id'],))
            conversation=await self.chat.conversation(conn,principal,turn['conversation_id'])
            if intent=='plugin_inspect':
                if args.get('plugin_id'):
                    row,release=await self.resolve(conn,principal.identity_id,args['plugin_id'])
                    data=public_version(row,True)
                    data['execution']=await self.execution_status(conn,principal.identity_id,node_id,release)
                    if args['plugin_id']=='builtin.mcp':
                        from nexus.plugins.mcp_contracts import MCPConfiguration
                        from nexus.core.plugins.mcp import model_list
                        data['registration_schema']=MCPConfiguration.model_json_schema()
                        data['registrations']=await model_list(conn,principal.identity_id,node_id)
                    if not row['builtin']: data.pop('release',None); data['manifest']=release.manifest.model_dump(mode='json')
                else:
                    items = await self.summaries(conn,principal.identity_id)
                    for item in items:
                        _, release = await self.resolve(conn,principal.identity_id,item['plugin_id'],item['digest'])
                        item['execution'] = await self.execution_status(conn,principal.identity_id,node_id,release)
                    data={'plugins':items, 'registry_is_not_execution_permission':True}
                await permissions.operation(conn,step['step_id'],'plugin-inspect:'+step['step_id'],utc_now())
                await self.chat.work.result(conn,fresh,{'status':'succeeded','verification':'plugin_registry_snapshot','data':data},utc_now())
                return
            entries=json.loads(turn['work_catalog_json'])
            pinned=next((item for item in entries if item.get('plugin_id')==args['plugin_id']),None)
            if pinned is None: raise AccessDenied('PLUGIN_NOT_IN_CATALOG')
            row,release=await self.resolve(conn,principal.identity_id,args['plugin_id'],pinned['digest'])
            tool=next((tool for tool in release.manifest.tools if tool.name==args['tool']),None)
            if tool is None: raise AccessDenied('PLUGIN_TOOL_UNAVAILABLE')
            if conversation['plan_mode'] and (tool.effect!='read' or not row['builtin']) and args['plugin_id'] not in {'builtin.interaction','builtin.todo'}:
                raise AccessDenied('PLAN_MODE_READ_ONLY')
            checked(tool.input_schema,args['input'])
            if (args['plugin_id'],args['tool']) in {('builtin.files','read_image'),('builtin.browser','browser_screenshot')} and not self.chat.config.chat_images_enabled:
                raise AccessDenied('MODEL_IMAGES_DISABLED')
            if not await self.permission(conn,fresh,grant,args['plugin_id'],tool): return
            mcp_binding=None
            if args['plugin_id']=='builtin.mcp':
                from nexus.core.plugins import mcp
                if args['tool'] in mcp.MANAGEMENT:
                    if args['tool']=='mcp_list': data=await mcp.model_list(conn,principal.identity_id,node_id,args['input'].get('server'))
                    else:
                        data=await mcp.remove(conn,principal.identity_id,node_id,args['input']['server'],args['input']['expected_revision'],fresh['step_id'],conversation['conversation_id'])
                        # Durable cancellation flags are part of the same commit.
                        from nexus.core.storage.control import cancel_task
                        for identifier in data.pop('cancel_task_ids',[]):
                            task=await load_task(conn,UUID(identifier))
                            await cancel_task(conn,task,principal.identity_id,utc_now())
                    await permissions.operation(conn,fresh['step_id'],'mcp-core:'+fresh['step_id'],utc_now())
                    await self.chat.work.result(conn,fresh,{'status':'succeeded','verification':'core_registry_snapshot','data':data},utc_now())
                    return
                if not fresh['task_id']:
                    # Static resource-only servers can keep their legacy path.
                    configured=await one(conn,"SELECT 1 FROM mcp_registrations WHERE owner_id=? AND node_id=? AND server=?",(str(principal.identity_id),str(node_id),args['input']['server']))
                    if configured or args['tool'] in mcp.DISCOVERY|{'mcp_call_tool'}:
                        mcp_binding=await mcp.prepare(conn,principal.identity_id,node_id,conversation['conversation_id'],fresh['step_id'],args['tool'],args['input'])
            manager_test=args['plugin_id']=='builtin.manager' and args['tool']=='plugin_manager' and args['input'].get('action')=='test'
            web_call=args['plugin_id']=='builtin.web'
            if release.manifest.runtime=='core' and not manager_test and not web_call:
                from nexus.core.plugins.core_tools import execute
                data=await execute(self,conn,principal,conversation,fresh,grant,args['plugin_id'],args['tool'],args['input'])
                if data is None: return
                await permissions.operation(conn,step['step_id'],'plugin-core:'+step['step_id'],utc_now())
                await self.chat.work.result(conn,fresh,{'status':'succeeded','verification':'core_record_snapshot','data':data},utc_now())
                return
            dependencies={item['plugin_id']:item['digest'] for item in entries if item.get('plugin_id') and item['plugin_id'] in RELEASES and RELEASES[item['plugin_id']].manifest.runtime=='node'}
        if web_call:
            await self.chat.work.web.start(step)
            return
        if manager_test:
            await self.test_step(step,principal,conversation['conversation_id'],args['input'].get('version_id'))
            return
        if not step['task_id']:
            programmatic=args['plugin_id']=='builtin.workflow' or not row['builtin']
            subcalls=(min(8,max(0,grant['max_operations']-grant['used_operations']-1)) if grant['operation_limits_enabled'] else 8) if programmatic else 0
            if args['plugin_id']=='builtin.workflow' and args['tool']=='workflow': subcalls=min(subcalls,len(args['input']['steps']))
            request=REQUEST.validate_json(canonical({'request_id':str(uuid5(NAMESPACE_URL,'plugin:'+step['step_id'])),
                'project_id':conversation['project_id'],'intent':'execute_plugin','arguments':PluginNodeArguments(
                    plugin_id=args['plugin_id'],tool=args['tool'],input=args['input'],release_digest=row['digest'],
                    context_id=UUID(conversation['conversation_id']),invocation_id=UUID(step['step_id']),
                    release=release if not row['builtin'] else None,capability_digests=dependencies if programmatic else {},max_subcalls=subcalls,mcp_binding=mcp_binding).model_dump(mode='json')}))
            async with self.chat.harness.operation:
                await self.chat.harness.submit(principal,node_id,request,work_step_id=UUID(step['step_id']),rate_limit=False)
            return
        async with self.database.transaction() as conn:
            task=await load_task(conn,UUID(step['task_id']))
            if task.status not in TERMINAL: return
            if args['plugin_id']=='builtin.mcp':
                from nexus.core.plugins.mcp import discovery_succeeded
                activated=await discovery_succeeded(conn,task)
            from nexus.core.cognitive.chat_work import task_feedback
            data=task_feedback(task)
            if args['plugin_id']=='builtin.mcp' and not activated and data['status']=='succeeded':
                data.update(status='failed',error_code='MCP_DISCOVERY_NOT_ACTIVATED')
            if task.execution_outcome=='unknown':
                await self.chat.work.abort(conn,step['turn_id'],'EXECUTION_UNKNOWN',utc_now()); return
            if data['status']=='succeeded':
                output=task.result.data
                if output.get('status')=='unknown':
                    await self.chat.work.abort(conn,step['turn_id'],'EXECUTION_UNKNOWN',utc_now());return
                if output.get('status') in {'failed','unknown','timed_out'} or output.get('exit_code',0) not in (None,0):
                    data.update(status='failed',error_code='PLUGIN_EXECUTION_FAILED')
                if args['plugin_id']=='builtin.present':
                    from nexus.core.plugins.deliverables import store
                    output=await store(conn,principal,conversation,task,output)
                elif args['plugin_id']=='builtin.mcp' and output.get('files'):
                    from nexus.core.plugins.deliverables import store
                    output={**output,'files':(await store(conn,principal,conversation,task,output))['files']}
                data['data']=output
            await self.chat.work.result(conn,step,data,utc_now())

    async def install_step(self,step,principal,args):
        release=PluginRelease.model_validate(args['release'])
        async with self.database.transaction() as conn:
            fresh,grant=await permissions.allowed_step(conn,step['step_id'],utc_now())
            turn=await one(conn,'SELECT conversation_id FROM chat_turns WHERE turn_id=?',(step['turn_id'],))
            conversation_id=turn[0]
        version=await self.install(principal,conversation_id,uuid5(NAMESPACE_URL,'plugin-install:'+step['step_id']),release,'nexus-generated',work_step_id=step['step_id'])
        if version['test_status'] in {'pending','running'}:
            if self.tester is None: raise AccessDenied('PLUGIN_TEST_UNAVAILABLE')
            test=await self.tester.start(principal,conversation_id,version['version_id'],step['step_id'])
            if test['status'] in {'pending','running'}: return
        async with self.database.transaction() as conn:
            fresh,_=await permissions.allowed_step(conn,step['step_id'],utc_now())
            row=await one(conn,'SELECT * FROM plugin_versions WHERE version_id=?',(version['version_id'],))
            if row['test_status']=='running': return
            await permissions.operation(conn,step['step_id'],'plugin-install:'+step['step_id'],utc_now())
            data=public_version(row)
            if row['enabled']:
                turn=await one(conn,'SELECT work_catalog_json FROM chat_turns WHERE turn_id=?',(step['turn_id'],))
                entries=[item for item in json.loads(turn[0]) if item.get('plugin_id')!=row['plugin_id']]
                entries.append({'name':'plugin_entry','plugin_id':row['plugin_id'],'digest':row['digest'],'description':release.manifest.description,'tools':[tool.name for tool in release.manifest.tools]})
                await conn.execute('UPDATE chat_turns SET work_catalog_json=? WHERE turn_id=?',(canonical(entries),step['turn_id']))
            data['test']=json.loads(row['test_json'])
            data['approval_required']=row['test_status']=='passed' and not row['approved']
            await self.chat.work.result(conn,fresh,{'status':'succeeded' if row['test_status']=='passed' else 'failed',
                'verification':'isolated_contract_cases' if row['test_status']=='passed' else None,'data':data},utc_now())

    async def test_step(self,step,principal,conversation_id,version_id):
        if not version_id: raise ValueError('test requires version_id')
        test=await self.tester.start(principal,conversation_id,version_id,step['step_id'])
        if test['status'] in {'pending','running'}: return
        async with self.database.transaction() as conn:
            fresh,_=await permissions.allowed_step(conn,step['step_id'],utc_now())
            row=await one(conn,'SELECT * FROM plugin_versions WHERE owner_id=? AND version_id=?',(str(principal.identity_id),version_id))
            await permissions.operation(conn,step['step_id'],'plugin-test:'+step['step_id'],utc_now())
            await self.chat.work.result(conn,fresh,{'status':'succeeded' if row['test_status']=='passed' else 'failed','data':public_version(row,True)},utc_now())
