"""Core-owned development records and compare-and-swap client deployments.

The authenticated, shipped client evaluates candidate JavaScript in an opaque
worker sandbox. It submits actual outputs; Core checks its own fixed suite.
No candidate code is imported or executed by this service.
"""

import json
from datetime import timedelta
from uuid import UUID, uuid4

from nexus.contracts.extensions import canonical
from nexus.contracts.ui_extensions import (
    RUNTIME, UIExtensionSource, build_bundle, check_outputs, gesture_suite,
)
from nexus.core.security import work as permissions
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, audit, timestamp, utc_now
from nexus.core.storage.tasks import Conflict
from nexus.core.plugins.ui_extension_management import record_event

def public(row):
    if row is None:
        return None
    result = {key: row[key] for key in ('version_id', 'plugin_id', 'version', 'bundle_digest', 'status', 'baseline_id', 'created_at')}
    result['test'] = json.loads(row['report_json']) if row['report_json'] else None
    result['runtime'] = json.loads(row['runtime_json']) if row['runtime_json'] else None
    return result


class UIExtensionService:
    def __init__(self, plugins):
        self.plugins = plugins
        self.chat = plugins.chat
        self.database = plugins.database

    async def policy(self, conn, principal, conversation_id):
        await self.chat.conversation(conn, principal, conversation_id)
        return await one(conn, 'SELECT * FROM ui_extension_policies WHERE conversation_id=? AND owner_id=?',
                         (str(conversation_id), str(principal.identity_id)))

    async def permitted(self, conn, row, *, development=False):
        self.plugins.require_enabled()
        policy = await one(conn, 'SELECT * FROM ui_extension_policies WHERE conversation_id=? AND owner_id=?',
                           (row['conversation_id'], row['owner_id']))
        owner = await one(conn, "SELECT 1 FROM identities WHERE identity_id=? AND role='owner' AND revoked_at IS NULL", (row['owner_id'],))
        conversation = await one(conn, 'SELECT archived,plan_mode FROM conversations WHERE conversation_id=?', (row['conversation_id'],))
        if not owner or not policy or not policy['enabled'] or not conversation or conversation['archived'] or conversation['plan_mode']:
            raise AccessDenied('UI_EXTENSION_POLICY_REQUIRED')
        if (await one(conn, 'SELECT paused FROM system_state WHERE singleton=1'))[0]:
            raise AccessDenied('BUSY')
        if development:
            if row['policy_revision'] != policy['revision']:
                raise AccessDenied('UI_EXTENSION_POLICY_CHANGED')
            grant = await permissions.step_grant(conn, row['work_step_id'])
            if grant is None:
                raise AccessDenied('WORK_STOPPED')
            await permissions.creation_step(conn, row['work_step_id'], utc_now(), scope=self.chat.harness.scope(UUID(grant['node_id'])))
        return policy

    async def configure(self, principal, conversation_id, value):
        self.plugins.require_enabled()
        async with self.chat.harness.operation, self.database.transaction() as conn:
            return await self.configure_in_transaction(conn, principal, conversation_id, value)

    async def configure_in_transaction(self, conn, principal, conversation_id, value, *, active_version_id=None):
        old = await self.policy(conn, principal, conversation_id)
        if (old['revision'] if old else 0) != value.expected_revision:
            raise Conflict('IDEMPOTENCY_CONFLICT')
        if old and old['active_version_id']:
            pending = await one(conn, "SELECT * FROM ui_extension_versions WHERE version_id=? AND status='observing'", (old['active_version_id'],))
            if pending:
                await self.rollback(conn, pending, old, 'policy_changed')
                old = await self.policy(conn, principal, conversation_id)
        client = await one(conn, 'SELECT * FROM ui_extension_clients WHERE owner_id=? AND client_id=?',
                           (str(principal.identity_id), str(value.client_id)))
        if value.enabled and (not client or client['runtime'] != RUNTIME or client['last_seen'] < timestamp(utc_now()-timedelta(seconds=15))):
            raise AccessDenied('UI_EXTENSION_CLIENT_UNAVAILABLE')
        # Changing target/namespace does not carry an old installation to it.
        active = old['active_version_id'] if old and old['client_id'] == str(value.client_id) and old['plugin_id'] == value.plugin_id else None
        if active_version_id is not None:
            active = active_version_id
        if not value.enabled:
            active = None
        revision = value.expected_revision+1
        deployment = (old['deployment_revision'] if old else 0)+1
        await conn.execute('''INSERT INTO ui_extension_policies VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(conversation_id) DO UPDATE SET client_id=excluded.client_id,plugin_id=excluded.plugin_id,
            profile=excluded.profile,revision=excluded.revision,enabled=excluded.enabled,active_version_id=excluded.active_version_id,
            deployment_revision=excluded.deployment_revision,updated_at=excluded.updated_at''',
            (str(conversation_id), str(principal.identity_id), str(value.client_id), value.plugin_id, value.profile,
             revision, int(value.enabled), active, deployment, timestamp(utc_now())))
        await conn.execute("UPDATE ui_extension_versions SET status='interrupted',updated_at=? WHERE conversation_id=? AND status IN ('built','testing')",
                           (timestamp(utc_now()), str(conversation_id)))
        await audit(conn, str(principal.identity_id), None, 'ui_extension.policy_changed', utc_now())
        await record_event(conn, principal.identity_id, conversation_id,
                           'enabled' if value.enabled and active_version_id else 'policy_enabled' if value.enabled else 'disabled',
                           old['active_version_id'] if old else None, active)
        return {'revision': revision, 'enabled': value.enabled, 'deployment_revision': deployment}

    async def inspect(self, conn, principal, conversation_id):
        policy = await self.policy(conn, principal, conversation_id)
        rows = await conn.execute_fetchall('SELECT * FROM ui_extension_versions WHERE owner_id=? AND conversation_id=? ORDER BY rowid DESC LIMIT 12',
                                          (str(principal.identity_id), str(conversation_id)))
        active = await one(conn, 'SELECT * FROM ui_extension_versions WHERE version_id=?', (policy['active_version_id'],)) if policy and policy['active_version_id'] else None
        return {
            'policy': dict(policy) if policy else None,
            'active': {**public(active), 'bundle': json.loads(active['bundle_json'])} if active else None,
            'versions': [public(row) for row in rows],
            'contract': {
                'runtime': RUNTIME,
                'entry': 'function update(frame, state) returns {commands: [...], state: {...}} synchronously. Initial state is {}. No imports, network, DOM, native bridge or persistent storage.',
                'frame': 'time_ms is monotonic milliseconds; hands has up to 2 items {x,y,pinch,open,landmarks}. x/y are mirrored normalized viewport coordinates. landmarks contains 21 {x,y,z} or [] in fixtures.',
                'commands': 'rotate {op,dx,dy}, zoom {op,delta}: finite values in [-200,200]. select {op,x,y}: [0,1]. reset {op}. Up to 4 commands/frame. State <=4096 UTF-8 bytes.',
                'behavior': 'One pinch moving rotates (dx/dy same sign as motion). Two pinches spreading zoom in (negative delta). A short stationary pinch released selects. Two open hands reset once until released. Idle/lost hands emit nothing. Reset state on tracking loss.',
                'suite': gesture_suite(),
                'submission': UIExtensionSource.model_json_schema(),
                'upgrade': 'Use active.version_id as base_version_id (null for first install). Submit a new version; Core builds a fixed bundle, waits for the authorized client tests, upgrades, then waits for actual runtime ready. Failed tests keep the old version. Runtime failure rolls back. Read failure evidence and revise source, never claim webcam validation from fixture tests.',
            },
        }

    async def work_step(self, step, principal, node_id, proposal):
        self.plugins.require_enabled()
        async with self.chat.harness.operation, self.database.transaction() as conn:
            fresh, grant = await permissions.allowed_step(conn, step['step_id'], utc_now(), scope=self.chat.harness.scope(node_id))
            if proposal['intent'] == 'inspect_ui_extension':
                data = await self.inspect(conn, principal, grant['conversation_id'])
                await permissions.operation(conn, step['step_id'], 'ui-inspect:'+step['step_id'], utc_now())
                await self.chat.work.result(conn, fresh, {'status': 'succeeded', 'verification': 'core_record_snapshot', 'data': data}, utc_now())
                return
            await permissions.creation_step(conn, step['step_id'], utc_now(), scope=self.chat.harness.scope(node_id))
            policy = await self.policy(conn, principal, grant['conversation_id'])
            if not policy or not policy['enabled']:
                raise AccessDenied('UI_EXTENSION_POLICY_REQUIRED')
            row = await one(conn, 'SELECT * FROM ui_extension_versions WHERE work_step_id=?', (step['step_id'],))
            if row is None:
                value = UIExtensionSource.model_validate_json(canonical(proposal['arguments']))
                if value.plugin_id != policy['plugin_id']:
                    raise AccessDenied('UI_EXTENSION_SCOPE_MISMATCH')
                if (str(value.base_version_id) if value.base_version_id else None) != policy['active_version_id']:
                    raise Conflict('IDEMPOTENCY_CONFLICT')
                client = await one(conn, 'SELECT * FROM ui_extension_clients WHERE owner_id=? AND client_id=?', (policy['owner_id'], policy['client_id']))
                if not client or client['runtime'] != RUNTIME or client['last_seen'] < timestamp(utc_now()-timedelta(seconds=15)):
                    raise AccessDenied('UI_EXTENSION_CLIENT_UNAVAILABLE')
                if await one(conn, "SELECT 1 FROM ui_extension_versions WHERE conversation_id=? AND status IN ('built','testing','observing')", (policy['conversation_id'],)):
                    raise AccessDenied('BUSY')
                if await one(conn, 'SELECT 1 FROM ui_extension_versions WHERE conversation_id=? AND plugin_id=? AND version=?', (policy['conversation_id'], value.plugin_id, value.version)):
                    raise Conflict('PLUGIN_VERSION_IMMUTABLE')
                usage = await one(conn, 'SELECT count(*),coalesce(sum(length(CAST(bundle_json AS BLOB))),0) FROM ui_extension_versions')
                if usage[0] >= self.plugins.config.max_versions or usage[1]+len(value.source.encode())+2000 > self.plugins.config.max_total_bytes:
                    raise AccessDenied('PLUGIN_STORAGE_FULL')
                bundle, digest = build_bundle(value)
                identifier, now = str(uuid4()), timestamp(utc_now())
                await permissions.operation(conn, step['step_id'], 'ui-build:'+step['step_id'], utc_now())
                await conn.execute('''INSERT INTO ui_extension_versions
                    (version_id,owner_id,conversation_id,work_step_id,plugin_id,version,policy_revision,baseline_id,bundle_json,bundle_digest,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'built',?,?)''',
                    (identifier, str(principal.identity_id), policy['conversation_id'], step['step_id'], value.plugin_id,
                     value.version, policy['revision'], policy['active_version_id'], canonical(bundle), digest, now, now))
                await record_event(conn, principal.identity_id, policy['conversation_id'], 'built', str(value.base_version_id) if value.base_version_id else None, identifier)
                await audit(conn, str(principal.identity_id), None, 'ui_extension.built', utc_now())
                row = await one(conn, 'SELECT * FROM ui_extension_versions WHERE version_id=?', (identifier,))
            if row['status'] in {'built','testing','observing'}:
                # A detached client cannot leave a tool step waiting forever.
                deadline = row['lease_expires_at'] or timestamp(utc_now())
                if row['status'] == 'built':
                    expired = row['created_at'] < timestamp(utc_now()-timedelta(seconds=30))
                else:
                    expired = deadline < timestamp(utc_now())
                if expired:
                    if row['status'] == 'observing':
                        await self.rollback(conn, row, policy, 'runtime_ready_timeout')
                    else:
                        await conn.execute("UPDATE ui_extension_versions SET status='interrupted',report_json=?,updated_at=? WHERE version_id=?",
                                           (canonical({'reason': 'client_evidence_timeout', 'verified': False}), timestamp(utc_now()), row['version_id']))
                    row = await one(conn, 'SELECT * FROM ui_extension_versions WHERE version_id=?', (row['version_id'],))
                else:
                    return
            await self.chat.work.result(conn, fresh, {
                'status': 'succeeded' if row['status'] == 'accepted' else 'failed',
                'verification': 'client_sandbox_fixtures_and_runtime' if row['status'] == 'accepted' else 'extension_not_accepted',
                'data': {**public(row), 'webcam_verified': False, 'installed': row['status'] == 'accepted'},
            }, utc_now())

    async def client_poll(self, principal, conversation_id, client):
        async with self.chat.harness.operation, self.database.transaction() as conn:
            policy = await self.policy(conn, principal, conversation_id)
            now = utc_now()
            await conn.execute('''INSERT INTO ui_extension_clients VALUES(?,?,?,?,?) ON CONFLICT(owner_id,client_id)
                DO UPDATE SET runtime=excluded.runtime,platform=excluded.platform,last_seen=excluded.last_seen''',
                (str(principal.identity_id), str(client.client_id), client.runtime, client.platform, timestamp(now)))
            latest = await one(conn, 'SELECT version,status FROM ui_extension_versions WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1', (str(conversation_id),))
            result = {'policy': dict(policy) if policy else None, 'active': None, 'test': None,
                      'latest': dict(latest) if latest else None}
            if not policy or policy['client_id'] != str(client.client_id):
                return result
            try:
                await self.permitted(conn, policy)
            except AccessDenied:
                return result
            candidate = await one(conn, "SELECT * FROM ui_extension_versions WHERE conversation_id=? AND status='built' ORDER BY rowid LIMIT 1", (str(conversation_id),))
            if candidate:
                try:
                    await self.permitted(conn, candidate, development=True)
                except AccessDenied:
                    await conn.execute("UPDATE ui_extension_versions SET status='interrupted' WHERE version_id=?", (candidate['version_id'],))
                else:
                    lease = str(uuid4())
                    await conn.execute("UPDATE ui_extension_versions SET status='testing',lease_id=?,lease_expires_at=?,updated_at=? WHERE version_id=?",
                                       (lease, timestamp(now+timedelta(seconds=30)), timestamp(now), candidate['version_id']))
                    result['test'] = {'version_id': candidate['version_id'], 'lease_id': lease, 'bundle': json.loads(candidate['bundle_json']),
                                      'bundle_digest': candidate['bundle_digest'], 'cases': gesture_suite()}
            active = await one(conn, 'SELECT * FROM ui_extension_versions WHERE version_id=?', (policy['active_version_id'],)) if policy['active_version_id'] else None
            if active and active['status'] in {'observing', 'accepted'}:
                result['active'] = {**public(active), 'bundle': json.loads(active['bundle_json']), 'deployment_revision': policy['deployment_revision']}
            return result

    async def test_report(self, principal, conversation_id, version_id, value):
        async with self.chat.harness.operation, self.database.transaction() as conn:
            policy = await self.policy(conn, principal, conversation_id)
            row = await one(conn, 'SELECT * FROM ui_extension_versions WHERE version_id=? AND owner_id=? AND conversation_id=?',
                            (str(version_id), str(principal.identity_id), str(conversation_id)))
            if not row or not policy or row['lease_id'] != str(value.lease_id) or policy['client_id'] != str(value.client_id) or row['bundle_digest'] != value.bundle_digest:
                raise AccessDenied('UI_EXTENSION_EVIDENCE_MISMATCH')
            encoded = canonical(value.model_dump(mode='json', exclude_none=True))
            if row['report_json']:
                previous = json.loads(row['report_json'])
                if previous.get('receipt') != json.loads(encoded):
                    raise Conflict('IDEMPOTENCY_CONFLICT')
                return public(row)
            await self.permitted(conn, row, development=True)
            if row['status'] != 'testing' or row['lease_expires_at'] < timestamp(utc_now()):
                raise AccessDenied('UI_EXTENSION_LEASE_EXPIRED')
            passed = not value.error and check_outputs(value.outputs)
            evidence = {'verification': 'client_sandbox_gesture_fixtures', 'passed': passed, 'webcam_verified': False,
                        'receipt': json.loads(encoded)}
            status = 'failed'
            if passed:
                if policy['active_version_id'] != row['baseline_id']:
                    raise Conflict('IDEMPOTENCY_CONFLICT')
                status = 'observing'
                await conn.execute('UPDATE ui_extension_policies SET active_version_id=?,deployment_revision=deployment_revision+1 WHERE conversation_id=?',
                                   (row['version_id'], str(conversation_id)))
            await conn.execute('UPDATE ui_extension_versions SET status=?,report_json=?,lease_expires_at=?,updated_at=? WHERE version_id=?',
                               (status, canonical(evidence), timestamp(utc_now()+timedelta(seconds=30)), timestamp(utc_now()), row['version_id']))
            await record_event(conn, principal.identity_id, conversation_id, 'published' if passed else 'test_failed',
                               row['baseline_id'], row['version_id'], value.error or ('' if passed else 'behavior_mismatch'))
            await audit(conn, str(principal.identity_id), None, 'ui_extension.'+status, utc_now())
            return public(await one(conn, 'SELECT * FROM ui_extension_versions WHERE version_id=?', (row['version_id'],)))

    async def rollback(self, conn, row, policy, reason):
        if policy['active_version_id'] != row['version_id']:
            return
        baseline = await one(conn, "SELECT * FROM ui_extension_versions WHERE version_id=? AND status='accepted'", (row['baseline_id'],)) if row['baseline_id'] else None
        fallback = baseline['version_id'] if baseline and baseline['conversation_id'] == policy['conversation_id'] and policy['enabled'] else None
        await conn.execute('UPDATE ui_extension_policies SET active_version_id=?,deployment_revision=deployment_revision+1 WHERE conversation_id=?', (fallback, policy['conversation_id']))
        await conn.execute("UPDATE ui_extension_versions SET status='rolled_back',runtime_json=?,updated_at=? WHERE version_id=?",
                           (canonical({'status': 'failed', 'reason': reason, 'restored_version_id': fallback}), timestamp(utc_now()), row['version_id']))
        await record_event(conn, row['owner_id'], row['conversation_id'], 'rolled_back', row['version_id'], fallback, reason)
        await audit(conn, row['owner_id'], None, 'ui_extension.rolled_back', utc_now())

    async def runtime_report(self, principal, conversation_id, value):
        async with self.chat.harness.operation, self.database.transaction() as conn:
            policy = await self.policy(conn, principal, conversation_id)
            if not policy or policy['client_id'] != str(value.client_id):
                raise AccessDenied('UI_EXTENSION_EVIDENCE_MISMATCH')
            row = await one(conn, 'SELECT * FROM ui_extension_versions WHERE version_id=? AND conversation_id=? AND owner_id=?',
                            (str(value.version_id), str(conversation_id), str(principal.identity_id)))
            if not row or row['bundle_digest'] != value.bundle_digest:
                raise AccessDenied('UI_EXTENSION_EVIDENCE_MISMATCH')
            # Duplicate or late reports never revert a later deployment.
            if policy['active_version_id'] != row['version_id'] or policy['deployment_revision'] != value.deployment_revision:
                return {'applied': False, 'version': public(row)}
            if value.status == 'failed':
                await self.rollback(conn, row, policy, value.error or 'client_runtime_failure')
            elif row['status'] == 'observing':
                await self.permitted(conn, row, development=True)
                if row['lease_expires_at'] < timestamp(utc_now()):
                    await self.rollback(conn, row, policy, 'runtime_ready_timeout')
                else:
                    await conn.execute("UPDATE ui_extension_versions SET status='accepted',runtime_json=?,updated_at=? WHERE version_id=?",
                                       (canonical(value.model_dump(mode='json')), timestamp(utc_now()), row['version_id']))
                    await record_event(conn, row['owner_id'], conversation_id, 'ready', after=row['version_id'])
                    await audit(conn, row['owner_id'], None, 'ui_extension.accepted', utc_now())
            elif row['status'] == 'accepted':
                await self.permitted(conn, row)
                evidence = canonical(value.model_dump(mode='json'))
                if row['runtime_json'] != evidence:
                    await conn.execute('UPDATE ui_extension_versions SET runtime_json=? WHERE version_id=?', (evidence, row['version_id']))
                    await record_event(conn, row['owner_id'], conversation_id, 'ready', after=row['version_id'])
            return {'applied': True, 'version': public(await one(conn, 'SELECT * FROM ui_extension_versions WHERE version_id=?', (row['version_id'],)))}

    async def reconcile(self):
        """Deterministic recovery; does not run a model or repeat unknown tests."""
        async with self.chat.harness.operation, self.database.transaction() as conn:
            rows = await conn.execute_fetchall("SELECT * FROM ui_extension_versions WHERE status IN ('built','testing','observing') LIMIT 256")
            for row in rows:
                policy = await one(conn, 'SELECT * FROM ui_extension_policies WHERE conversation_id=?', (row['conversation_id'],))
                try:
                    await self.permitted(conn, row, development=True)
                    expired = (row['lease_expires_at'] < timestamp(utc_now()) if row['lease_expires_at']
                               else row['created_at'] < timestamp(utc_now()-timedelta(seconds=30)))
                except AccessDenied:
                    expired = True
                if expired:
                    if row['status'] == 'observing' and policy:
                        await self.rollback(conn, row, policy, 'development_interrupted')
                    else:
                        await conn.execute("UPDATE ui_extension_versions SET status='interrupted',updated_at=? WHERE version_id=?", (timestamp(utc_now()), row['version_id']))
