"""Owner-maintained business cases and evidence-bound plugin regression rollback."""

import json
from datetime import timedelta
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt

from nexus.contracts.base import StrictContract
from nexus.contracts.extensions import canonical, digest_value
from nexus.plugins.contracts import PluginCase, PluginRelease
from nexus.core.security.modes import effective
from nexus.core.security.types import AccessDenied, Principal
from nexus.core.storage.database import audit, one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict


class BusinessSuite(StrictContract):
    expected_revision: StrictInt = Field(ge=0)
    cases: list[PluginCase] = Field(min_length=1, max_length=4)
    rollback_on_failure: StrictBool = False


async def configure(conn, owner_id, plugin_id, value: BusinessSuite):
    if not plugin_id.startswith('user.'):
        raise AccessDenied('PLUGIN_NAMESPACE_RESERVED')
    encoded = canonical([case.model_dump(mode='json') for case in value.cases])
    if len(encoded.encode()) > 12000:
        raise ValueError('business cases exceed limit')
    from nexus.contracts.basic import relative_path
    for case in value.cases:
        for path in case.files:
            relative_path(path)
    old = await one(conn, 'SELECT * FROM plugin_business_suites WHERE owner_id=? AND plugin_id=?', (str(owner_id), plugin_id))
    if value.expected_revision != (old['revision'] if old else 0):
        raise Conflict('business suite changed')
    if not old and (await one(conn,'SELECT count(*) FROM plugin_business_suites'))[0]>=1000:
        raise AccessDenied('PLUGIN_TEST_STORAGE_FULL')
    await conn.execute('''INSERT INTO plugin_business_suites VALUES(?,?,1,?,?,?) ON CONFLICT(owner_id,plugin_id)
        DO UPDATE SET revision=revision+1,cases_json=excluded.cases_json,digest=excluded.digest,rollback_on_failure=excluded.rollback_on_failure''',
                       (str(owner_id), plugin_id, encoded, digest_value(json.loads(encoded)), int(value.rollback_on_failure)))
    await audit(conn, str(owner_id), None, 'plugin.business_suite_changed', utc_now())
    return {'revision':value.expected_revision+1,'digest':digest_value(json.loads(encoded)), 'host_safety_verified':False}


async def require_business(conn, row):
    suite = await one(conn, 'SELECT * FROM plugin_business_suites WHERE owner_id=? AND plugin_id=?', (row['owner_id'], row['plugin_id']))
    if suite:
        result = json.loads(row['test_json'])
        if not result.get('business_verified') or result.get('suite_digest') != suite['digest'] or result.get('suite_revision') != suite['revision']:
            raise AccessDenied('PLUGIN_BUSINESS_TEST_REQUIRED')
    return suite


async def start_rollout(conn, row, baseline, conversation_id, mode):
    suite = await require_business(conn, row)
    if not suite or not suite['rollback_on_failure'] or baseline is None or baseline['version_id'] == row['version_id']:
        return
    if await one(conn,'SELECT 1 FROM plugin_rollouts WHERE version_id=?',(row['version_id'],)):
        raise AccessDenied('PLUGIN_ROLLBACK_REQUIRES_NEW_VERSION')
    before = PluginRelease.model_validate_json(baseline['release_json'])
    after = PluginRelease.model_validate_json(row['release_json'])
    if before.manifest.tools != after.manifest.tools or before.manifest.dependencies != after.manifest.dependencies:
        raise AccessDenied('PLUGIN_ROLLBACK_COMPATIBILITY_REQUIRED')
    comparison=json.loads(row['test_json']).get('comparison',{})
    if comparison.get('baseline_id')!=baseline['version_id'] or comparison.get('baseline_digest')!=baseline['digest']:
        raise AccessDenied('PLUGIN_BASELINE_CHANGED')
    if not comparison.get('case_count') or comparison.get('baseline_passed')!=comparison['case_count']:
        raise AccessDenied('PLUGIN_BASELINE_BUSINESS_TEST_FAILED')
    await conn.execute("UPDATE plugin_rollouts SET status='blocked' WHERE owner_id=? AND status='observing' AND version_id IN (SELECT version_id FROM plugin_versions WHERE plugin_id=?)", (row['owner_id'],row['plugin_id']))
    cursor=(await one(conn,"SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
    await conn.execute("""INSERT INTO plugin_rollouts(version_id,baseline_id,owner_id,conversation_id,permission_fingerprint,cancel_cursor,suite_revision,decision_revision,status,created_at)
        VALUES(?,?,?,?,?,?,?,?,'observing',?) ON CONFLICT(version_id) DO NOTHING""",
                       (row['version_id'], baseline['version_id'], row['owner_id'], str(conversation_id), mode['fingerprint'], cursor, suite['revision'], row['decision_revision']+1, timestamp(utc_now())))


async def observe(service):
    if not service.config.enabled:
        return
    async with service.chat.harness.operation, service.database.transaction() as conn:
        if (await one(conn,'SELECT paused FROM system_state WHERE singleton=1'))[0]:
            return
        rows = await conn.execute_fetchall("SELECT r.*,v.plugin_id,v.digest,v.enabled,v.decision_revision AS current_revision FROM plugin_rollouts r JOIN plugin_versions v USING(version_id) WHERE r.status IN ('observing','quarantined') LIMIT 100")
        for row in rows:
            suite = await one(conn, 'SELECT * FROM plugin_business_suites WHERE owner_id=? AND plugin_id=?', (row['owner_id'],row['plugin_id']))
            mode = await effective(conn,row['conversation_id'])
            owner = await one(conn,"SELECT token_hash FROM identities WHERE identity_id=? AND role='owner' AND revoked_at IS NULL",(row['owner_id'],))
            conversation=await one(conn,'SELECT archived,plan_mode FROM conversations WHERE conversation_id=?',(row['conversation_id'],))
            cursor=(await one(conn,"SELECT coalesce(max(event_id),0) FROM control_events WHERE kind='cancel_all'"))[0]
            if not owner or (not row['enabled'] and row['status']!='quarantined') or row['current_revision']!=row['decision_revision'] or cursor!=row['cancel_cursor'] or not suite or not suite['rollback_on_failure'] or suite['revision']!=row['suite_revision'] or mode['fingerprint']!=row['permission_fingerprint'] or conversation['archived'] or conversation['plan_mode']:
                await conn.execute("UPDATE plugin_rollouts SET status='blocked' WHERE version_id=?",(row['version_id'],))
                continue
            tasks = await conn.execute_fetchall("""SELECT * FROM tasks WHERE owner_id=? AND created_at>=? AND json_extract(action_json,'$.tool')='plugin.call'
                AND json_extract(action_json,'$.arguments.release_digest')=? AND json_extract(action_json,'$.arguments.context_id')=?
                AND execution_outcome!='unknown' AND status IN ('succeeded','failed','cancelled','timed_out')
                AND task_id NOT IN (SELECT task_id FROM plugin_rollout_tasks WHERE version_id=?) ORDER BY rowid LIMIT 20""",
                (row['owner_id'],row['created_at'],row['digest'],row['conversation_id'],row['version_id']))
            failures = row['failures']; observations = row['observations']
            for task in tasks:
                # Unknown results or cancellation do not diagnose a bad version.
                if task['status']=='cancelled' or task['execution_outcome']=='not_started':
                    await conn.execute('INSERT INTO plugin_rollout_tasks VALUES(?,?,-1)',(row['version_id'],task['task_id']))
                    continue
                if await one(conn,'SELECT 1 FROM task_attempts WHERE task_id=? AND execution_stopped=0',(task['task_id'],)):
                    continue
                result=json.loads(task['result_json'] or '{}'); data=result.get('data',{})
                passed=task['status']=='succeeded' and data.get('status') not in {'failed','unknown','timed_out'} and data.get('exit_code',0) in (None,0)
                arguments=json.loads(task['action_json'])['arguments']
                for case in json.loads(suite['cases_json']):
                    if case['tool']==arguments.get('tool') and canonical(case['input'])==canonical(arguments.get('input')):
                        passed=passed and canonical(data)==canonical(case['expected'])
                await conn.execute('INSERT INTO plugin_rollout_tasks VALUES(?,?,?)',(row['version_id'],task['task_id'],int(passed)))
                observations+=1; failures+=int(not passed)
            await conn.execute('UPDATE plugin_rollouts SET observations=?,failures=? WHERE version_id=?',(observations,failures,row['version_id']))
            if failures:
                if row['status']=='observing':
                    await conn.execute('UPDATE plugin_versions SET enabled=0,decision_revision=decision_revision+1 WHERE version_id=?',(row['version_id'],))
                    await conn.execute("UPDATE plugin_rollouts SET status='quarantined',decision_revision=decision_revision+1 WHERE version_id=?",(row['version_id'],))
                    await audit(conn,row['owner_id'],None,'plugin.business_quarantined',utc_now())
                active=await one(conn,"SELECT 1 FROM tasks WHERE owner_id=? AND json_extract(action_json,'$.arguments.release_digest')=? AND (status NOT IN ('succeeded','failed','cancelled','timed_out') OR execution_outcome='unknown' OR EXISTS(SELECT 1 FROM task_attempts a WHERE a.task_id=tasks.task_id AND execution_stopped=0)) LIMIT 1",(row['owner_id'],row['digest']))
                if active:
                    continue
                baseline=await one(conn,'SELECT * FROM plugin_versions WHERE version_id=?',(row['baseline_id'],))
                # A user revocation of the fallback is never undone by recovery.
                decision=await one(conn,'SELECT decision FROM plugin_decisions WHERE version_id=? ORDER BY rowid DESC LIMIT 1',(row['baseline_id'],))
                evidence=json.loads(baseline['test_json'])
                environments=[service.tester.tools.extensions.sandbox_environment(node) for node in service.chat.harness.link.config.sandbox_nodes]
                if not baseline['approved'] or baseline['test_status']!='passed' or decision and decision[0]=='disable' or evidence.get('environment') not in environments or evidence.get('release_digest')!=baseline['digest']:
                    await conn.execute("UPDATE plugin_rollouts SET status='blocked' WHERE version_id=?",(row['version_id'],))
                    await conn.execute('UPDATE plugin_versions SET enabled=0,decision_revision=decision_revision+1 WHERE version_id=?',(row['version_id'],))
                    continue
                await conn.execute('UPDATE plugin_versions SET enabled=0,decision_revision=decision_revision+1 WHERE version_id=? AND enabled=1',(row['version_id'],))
                await conn.execute('UPDATE plugin_versions SET enabled=1,decision_revision=decision_revision+1 WHERE version_id=?',(row['baseline_id'],))
                await conn.execute("UPDATE plugin_rollouts SET status='rolled_back' WHERE version_id=?",(row['version_id'],))
                await audit(conn,row['owner_id'],None,'plugin.business_rollback',utc_now())
            elif observations>=10 and row['created_at']<=timestamp(utc_now()-timedelta(minutes=30)):
                await conn.execute("UPDATE plugin_rollouts SET status='accepted' WHERE version_id=?",(row['version_id'],))
