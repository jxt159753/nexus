"""Plugin contract tests reuse authenticated runsc, receipts and recovery.

Core compares actual output with declared cases; this is explicitly not proof
of business correctness or arbitrary plugin safety. Candidate code never runs
in Core or the ordinary Node before it has a successful isolated test.
"""

import asyncio
import json
import sqlite3
import time
from uuid import NAMESPACE_URL,UUID,uuid4,uuid5

from nexus.contracts.extensions import canonical,digest_value
from nexus.core.extensions.executions import SandboxExecutions
from nexus.core.security.modes import effective
from nexus.core.security.work import allowed_step, creation_step
from nexus.core.security.types import AccessDenied,Principal
from nexus.core.storage.database import one,timestamp,utc_now,audit
from nexus.plugins.contracts import PluginRelease, PluginCase
from nexus.plugins.schema import checked


def test_source(release):
    # The SDK stub is trusted source; all candidate code stays inside runsc.
    source=repr(release.source)
    return '''import json,sys,types,pathlib
class PluginContext:
    def __init__(self,workspace,testing=True): self.workspace=pathlib.Path(workspace); self.testing=testing
    def call(self,*a,**k): raise RuntimeError("host tool calls unavailable in isolated contract tests")
sdk=types.ModuleType("nexus_plugin"); sdk.PluginContext=PluginContext; sys.modules["nexus_plugin"]=sdk
case=json.load(sys.stdin)
root=pathlib.Path("/tmp/workspace"); root.mkdir(exist_ok=True)
for name,content in case.get("files",{}).items():
    path=root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(content,encoding="utf-8")
namespace={"__name__":"nexus_plugin_candidate"}
exec(compile('''+source+''',"plugin.py","exec"),namespace)
result=namespace["handle"](case["tool"],case["input"],PluginContext(root))
print(json.dumps(result,ensure_ascii=False,separators=(",",":"),allow_nan=False))
'''


class PluginTester:
    def __init__(self,service,transport,tools):
        self.service=service;self.database=service.database;self.transport=transport;self.tools=tools
        self.journal=SandboxExecutions(self.database,10000,table='plugin_sandbox_executions')
        self.active={}
        previous_result=transport.result_handler
        previous_reconcile=transport.reconcile_handler
        async def receive(node,result):
            if await self.journal.get(result.execution_id):
                await self.receive(node,result)
            elif previous_result: await previous_result(node,result)
            else: raise AccessDenied('SANDBOX_EVIDENCE_MISMATCH')
        async def reconcile(node):
            await asyncio.gather(self.reconcile(node),previous_reconcile(node) if previous_reconcile else asyncio.sleep(0))
        transport.result_handler=receive
        transport.reconcile_handler=reconcile

    async def start(self,principal,conversation_id,version_id,step_id=None):
        self.service.require_enabled()
        async with self.service.chat.harness.operation,self.database.transaction() as conn:
            await self.service.chat.conversation(conn,principal,conversation_id)
            row=await one(conn,'SELECT * FROM plugin_versions WHERE owner_id=? AND version_id=? AND builtin=0',(str(principal.identity_id),str(version_id)))
            if row is None: raise LookupError('user plugin version not found')
            if step_id:
                _, grant = await allowed_step(conn, step_id, utc_now())
                await creation_step(conn, step_id, utc_now(), scope=self.service.chat.harness.scope(UUID(grant['node_id'])))
                if grant['conversation_id'] != str(conversation_id): raise AccessDenied('POLICY_DENIED')
                previous=await one(conn,'SELECT * FROM plugin_test_runs WHERE version_id=? AND work_step_id=? ORDER BY rowid DESC LIMIT 1',(str(version_id),str(step_id)))
                if previous: return dict(previous)
            suite=await one(conn,'SELECT * FROM plugin_business_suites WHERE owner_id=? AND plugin_id=?',(row['owner_id'],row['plugin_id']))
            saved=json.loads(row['test_json'])
            if row['test_status']=='passed' and (not suite or saved.get('suite_digest')==suite['digest'] and saved.get('suite_revision')==suite['revision']): return {'status':'passed','version_id':str(version_id)}
            existing=await one(conn,"SELECT * FROM plugin_test_runs WHERE version_id=? AND status IN ('pending','running','unknown')",(str(version_id),))
            if existing: return dict(existing)
            if (await one(conn,'SELECT count(*) FROM plugin_test_runs'))[0]>=1000: raise AccessDenied('PLUGIN_TEST_STORAGE_FULL')
            environment=None;node=None
            for identifier in self.service.chat.harness.link.config.sandbox_nodes:
                status=self.transport.status(identifier)
                if status.get('available'):
                    node=identifier
                    environment=self.tools.extensions.sandbox_environment(identifier)
                    break
            if node is None:
                await conn.execute("UPDATE plugin_versions SET test_status='unavailable',test_json=? WHERE version_id=?",(canonical({'reason':'qualified_sandbox_unavailable'}),str(version_id)))
                return {'status':'unavailable','reason':'qualified_sandbox_unavailable'}
            mode=await effective(conn,conversation_id); now=timestamp(utc_now()); identifier=str(uuid4())
            cursor=(await one(conn,"SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
            await conn.execute("INSERT INTO plugin_test_runs(test_run_id,version_id,owner_id,conversation_id,work_step_id,node_id,environment_json,permission_fingerprint,decision_revision,cancel_cursor,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (identifier,str(version_id),str(principal.identity_id),str(conversation_id),str(step_id) if step_id else None,str(node),canonical(environment),mode['fingerprint'],row['decision_revision'],cursor,now,now))
            await conn.execute("UPDATE plugin_versions SET test_status='running' WHERE version_id=?",(str(version_id),))
            if suite:
                baseline=await one(conn,'SELECT version_id FROM plugin_versions WHERE owner_id=? AND plugin_id=? AND enabled=1 AND version_id!=?',(row['owner_id'],row['plugin_id'],str(version_id)))
                await conn.execute('UPDATE plugin_test_runs SET suite_revision=?,suite_digest=?,independent_cases_json=?,baseline_id=? WHERE test_run_id=?',
                    (suite['revision'],suite['digest'],suite['cases_json'],baseline[0] if baseline else None,identifier))
            return {'test_run_id':identifier,'status':'pending'}

    async def allowed(self,run,*,starting=False):
        if not self.service.config.enabled: return False
        async with self.database.transaction(write=False) as conn:
            owner=await one(conn,'SELECT revoked_at FROM identities WHERE identity_id=?',(run['owner_id'],))
            conversation=await one(conn,'SELECT archived FROM conversations WHERE conversation_id=?',(run['conversation_id'],))
            if owner is None or owner[0] or conversation is None or conversation[0]: return False
            mode=await effective(conn,run['conversation_id'])
            if mode['fingerprint']!=run['permission_fingerprint']: return False
            row=await one(conn,'SELECT decision_revision FROM plugin_versions WHERE version_id=?',(run['version_id'],))
            cursor=(await one(conn,"SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
            if row is None or row[0]!=run['decision_revision'] or cursor!=run['cancel_cursor']: return False
            suite=await one(conn,'SELECT s.* FROM plugin_business_suites s JOIN plugin_versions v ON v.owner_id=s.owner_id AND v.plugin_id=s.plugin_id WHERE v.version_id=?',(run['version_id'],))
            if (suite['revision'] if suite else 0)!=run['suite_revision'] or (suite['digest'] if suite else None)!=run['suite_digest']: return False
            if starting and (await one(conn,'SELECT paused FROM system_state WHERE singleton=1'))[0]: return False
            if run['work_step_id']:
                try:
                    _, grant = await allowed_step(conn,run['work_step_id'],utc_now())
                    await creation_step(conn,run['work_step_id'],utc_now(),scope=self.service.chat.harness.scope(UUID(grant['node_id'])))
                except AccessDenied: return False
            return True

    async def process(self,run,cancel):
        async with self.database.transaction() as conn:
            row=await one(conn,'SELECT * FROM plugin_versions WHERE version_id=?',(run['version_id'],))
            release=PluginRelease.model_validate_json(row['release_json'])
            baseline=await one(conn,'SELECT * FROM plugin_versions WHERE version_id=?',(run['baseline_id'],)) if run['baseline_id'] else None
            await conn.execute("UPDATE plugin_test_runs SET status='running',updated_at=? WHERE test_run_id=?",(timestamp(utc_now()),run['test_run_id']))
        records=[];status='failed';environment=json.loads(run['environment_json']);started=time.monotonic()
        try:
            if not release.tests or {case.tool for case in release.tests}!={tool.name for tool in release.manifest.tools}:
                raise ValueError('at least one contract case is required for every tool')
            if release.manifest.dependencies: raise ValueError('isolated image does not provide declared third-party dependencies')
            independent=[PluginCase.model_validate(item) for item in json.loads(run['independent_cases_json'])]
            cases=[('contract',release,case) for case in release.tests]+[('business',release,case) for case in independent]
            if baseline:
                baseline_release=PluginRelease.model_validate_json(baseline['release_json'])
                cases += [('baseline',baseline_release,case) for case in independent]
            for index,(kind,candidate,case) in enumerate(cases):
                if cancel.is_set() or time.monotonic()-started>60 or not await self.allowed(run,starting=True):
                    status='interrupted';break
                for path in case.files:
                    from nexus.contracts.basic import relative_path
                    relative_path(path)
                tool=next((tool for tool in candidate.manifest.tools if tool.name==case.tool),None)
                if tool is None: raise ValueError('business suite references an unavailable tool')
                checked(tool.input_schema,case.input);checked(tool.output_schema,case.expected)
                proxy={'kind':'tool','source':test_source(candidate),'environment':environment}
                proxy_json=canonical(proxy)
                version={'version_id':run['version_id'],'artifact_digest':digest_value(proxy),'environment_digest':digest_value(environment),
                         'release_json':proxy_json,'environment_json':canonical(environment)}
                identifier=uuid5(NAMESPACE_URL,f"plugin-test:{run['test_run_id']}:{index}")
                input_value={'tool':case.tool,'input':case.input,'files':case.files}
                record=await self.journal.prepare(UUID(run['node_id']),version,input_value,10,execution_id=identifier,test_run_id=run['test_run_id'])
                if record['status'] in {'sent','unknown'}:
                    status='unknown';break
                if record['status']=='settled': reply=json.loads(record['result_json'])
                else:
                    await self.journal.sent(identifier)
                    reply=await self.transport.execute(UUID(run['node_id']),version,input_value,10,execution_id=identifier,cancel_event=cancel,permitted=lambda:self.allowed(run,starting=True))
                    stored=await self.journal.get(identifier)
                    if stored['status']=='settled': reply=json.loads(stored['result_json'])
                    else: await self.journal.store(UUID(run['node_id']),identifier,version['artifact_digest'],reply)
                if reply.get('outcome')!='succeeded':
                    status='unknown' if reply.get('outcome')=='unknown' else 'failed'
                    records.append({'case':index,'kind':kind,'status':status,'error':reply.get('error_code')})
                    if kind=='baseline' and status!='unknown': continue
                    break
                output=reply['output'];checked(tool.output_schema,output)
                matches=canonical(output)==canonical(case.expected)
                records.append({'case':index,'kind':kind,'matched':matches,'actual':output})
                if not matches and kind!='baseline': status='failed';break
            else:
                status='passed'
        except (ValueError,KeyError,LookupError) as exc:
            status='failed';records.append({'error':str(exc)[:500]})
        except AccessDenied as exc:
            status='unavailable';records.append({'error':exc.code})
        except asyncio.CancelledError:
            cancel.set();status='interrupted';raise
        finally:
            permitted=await self.allowed(run)
            business_passed=bool(run['suite_revision'] and status=='passed' and all(item.get('matched') for item in records if item.get('kind')=='business'))
            result={'verification':'isolated_business_cases' if business_passed else 'isolated_contract_cases','business_verified':business_passed,
                    'host_safety_verified':False,'suite_revision':run['suite_revision'],'suite_digest':run['suite_digest'],
                    'environment':environment,'release_digest':row['digest'],'cases':records,
                    'comparison':{'baseline_id':run['baseline_id'],'baseline_digest':baseline['digest'] if baseline else None,
                                  'baseline_passed':sum(item.get('matched',False) for item in records if item.get('kind')=='baseline'),
                                  'candidate_passed':sum(item.get('matched',False) for item in records if item.get('kind')=='business'),
                                  'case_count':len(json.loads(run['independent_cases_json']))}}
            async with self.service.chat.harness.operation,self.database.transaction() as conn:
                await conn.execute('UPDATE plugin_test_runs SET status=?,result_json=?,updated_at=? WHERE test_run_id=?',(status,canonical(result),timestamp(utc_now()),run['test_run_id']))
                await conn.execute('UPDATE plugin_versions SET test_status=?,test_json=? WHERE version_id=?',('interrupted' if status=='unknown' else status,canonical(result),run['version_id']))
                mode=await effective(conn,run['conversation_id'])
                if status=='passed' and permitted and mode['effective_mode']=='full' and mode['fingerprint']==run['permission_fingerprint']:
                    fresh=await one(conn,'SELECT * FROM plugin_versions WHERE version_id=?',(run['version_id'],))
                    identity=await one(conn,'SELECT token_hash FROM identities WHERE identity_id=? AND revoked_at IS NULL',(run['owner_id'],))
                    cursor=(await one(conn,"SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
                    conversation=await one(conn,'SELECT archived FROM conversations WHERE conversation_id=?',(run['conversation_id'],))
                    if identity is not None and not conversation[0] and fresh['decision_revision']==run['decision_revision'] and cursor==run['cancel_cursor']:
                        principal=Principal(UUID(run['owner_id']),'owner',identity[0])
                        await conn.execute('SAVEPOINT automatic_enable')
                        try:
                            await self.service.decide(conn,principal,fresh,run['conversation_id'],'enable',automatic=True)
                        except AccessDenied as exc:
                            await conn.execute('ROLLBACK TO automatic_enable')
                            result['enable_blocked']=exc.code
                            await conn.execute('UPDATE plugin_test_runs SET result_json=? WHERE test_run_id=?',(canonical(result),run['test_run_id']))
                            await conn.execute('UPDATE plugin_versions SET test_json=? WHERE version_id=?',(canonical(result),run['version_id']))
                        finally:
                            await conn.execute('RELEASE automatic_enable')

    async def pump(self):
        if not self.service.config.enabled: return
        for identifier,(task,cancel) in list(self.active.items()):
            if task.done():
                await task;del self.active[identifier]
        if self.active: return
        async with self.database.transaction(write=False) as conn:
            row=await one(conn,"SELECT * FROM plugin_test_runs WHERE status='pending' ORDER BY rowid LIMIT 1")
        if row:
            cancel=asyncio.Event()
            self.active[row['test_run_id']]=(asyncio.create_task(self.process(row,cancel)),cancel)

    async def receive(self,node,result):
        reply={'outcome':'succeeded','output':json.loads(result.output_json)} if result.outcome=='succeeded' and result.stopped else {'outcome':result.outcome if result.stopped else 'unknown','error_code':result.error_code or 'SANDBOX_STOP_UNKNOWN','stopped':result.stopped}
        await self.journal.store(node,result.execution_id,result.artifact_digest,reply)
        await self.transport.acknowledge(node,result.execution_id,result.artifact_digest)

    async def reconcile(self,node):
        while True:
            for record in await self.journal.unsettled(node):
                if not await self.journal.claim_query(record['execution_id']): continue
                answer=await self.transport.query(node,UUID(record['execution_id']))
                if answer is None: continue
                if answer.status!='not_found' and answer.command_digest!=record['command_digest']: raise AccessDenied('SANDBOX_EVIDENCE_MISMATCH')
                if answer.result: await self.receive(node,answer.result)
                elif answer.status=='not_found':
                    await self.journal.store(node,UUID(record['execution_id']),record['artifact_digest'],{'outcome':'failed','error_code':'SANDBOX_NOT_RECEIVED','stopped':True})
            await asyncio.sleep(2)

    async def recover(self):
        async with self.database.transaction() as conn:
            await conn.execute("UPDATE plugin_test_runs SET status='interrupted',updated_at=? WHERE status IN ('pending','running')",(timestamp(utc_now()),))
            await conn.execute("UPDATE plugin_versions SET test_status='interrupted' WHERE test_status='running'")

    async def run(self):
        try:
            while True:
                await self.pump()
                for identifier,(task,cancel) in list(self.active.items()):
                    async with self.database.transaction(write=False) as conn:
                        run=await one(conn,'SELECT * FROM plugin_test_runs WHERE test_run_id=?',(identifier,))
                    if not await self.allowed(run): cancel.set()
                await asyncio.sleep(.1)
        finally:
            for task,cancel in self.active.values(): cancel.set();task.cancel()
            await asyncio.gather(*(task for task,_ in self.active.values()),return_exceptions=True)
