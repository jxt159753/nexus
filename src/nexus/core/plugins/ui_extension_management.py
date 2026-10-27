"""Owner management views over the existing isolated client lifecycle."""

import json
from datetime import timedelta

from nexus.contracts.ui_extensions import UIExtensionPolicy
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict


async def record_event(conn, owner_id, conversation_id, kind, before=None, after=None, reason=''):
    await conn.execute('''INSERT INTO ui_extension_events
        (owner_id,conversation_id,kind,from_version_id,to_version_id,reason,created_at)
        VALUES(?,?,?,?,?,?,?)''',
        (str(owner_id), str(conversation_id), kind, before, after, reason[:500], timestamp(utc_now())))


def version_summary(row):
    test = json.loads(row['report_json']) if row['report_json'] else {}
    return {
        **{key: row[key] for key in ('version_id', 'plugin_id', 'version', 'bundle_digest', 'status', 'baseline_id', 'created_at', 'updated_at')},
        'name': json.loads(row['bundle_json'])['name'],
        'test': {'passed': test.get('passed'), 'error': test.get('receipt', {}).get('error', ''),
                 'verification': test.get('verification'), 'webcam_verified': False},
        'runtime': json.loads(row['runtime_json']) if row['runtime_json'] else None,
    }


async def catalog(service, conn, principal):
    await service.chat.owner(conn, principal)
    rows = await conn.execute_fetchall('''SELECT v.*,c.title AS conversation_title,c.archived,
        p.enabled AS policy_enabled,p.active_version_id,p.client_id,p.revision,p.deployment_revision
        FROM ui_extension_versions v JOIN conversations c ON c.conversation_id=v.conversation_id
        LEFT JOIN ui_extension_policies p ON p.conversation_id=v.conversation_id AND p.plugin_id=v.plugin_id
        WHERE v.owner_id=? AND v.rowid=(SELECT max(x.rowid) FROM ui_extension_versions x
            WHERE x.conversation_id=v.conversation_id AND x.plugin_id=v.plugin_id)
        ORDER BY v.rowid DESC''', (str(principal.identity_id),))
    result = []
    for row in rows:
        active = await one(conn, 'SELECT * FROM ui_extension_versions WHERE version_id=?', (row['active_version_id'],)) if row['active_version_id'] else None
        result.append({
            'kind': 'visualization', 'plugin_id': row['plugin_id'], 'name': json.loads(row['bundle_json'])['name'],
            'conversation_id': row['conversation_id'], 'conversation_title': row['conversation_title'],
            'archived': bool(row['archived']), 'client_id': row['client_id'],
            'enabled': bool(row['policy_enabled'] and active), 'development_enabled': bool(row['policy_enabled']),
            'active': version_summary(active) if active else None, 'latest': version_summary(row),
        })
    return result


async def detail(service, conn, principal, conversation_id, *, before_version=2**63-1, before_event=2**63-1):
    policy = await service.policy(conn, principal, conversation_id)
    conversation = await service.chat.conversation(conn, principal, conversation_id)
    rows = await conn.execute_fetchall('''SELECT rowid AS cursor,* FROM ui_extension_versions
        WHERE owner_id=? AND conversation_id=? AND rowid<? ORDER BY rowid DESC LIMIT 51''',
        (str(principal.identity_id), str(conversation_id), before_version))
    events = await conn.execute_fetchall('''SELECT e.*,a.version AS from_version,b.version AS to_version
        FROM ui_extension_events e LEFT JOIN ui_extension_versions a ON a.version_id=e.from_version_id
        LEFT JOIN ui_extension_versions b ON b.version_id=e.to_version_id
        WHERE e.owner_id=? AND e.conversation_id=? AND e.event_id<? ORDER BY e.event_id DESC LIMIT 51''',
        (str(principal.identity_id), str(conversation_id), before_event))
    active = await one(conn, 'SELECT * FROM ui_extension_versions WHERE version_id=?', (policy['active_version_id'],)) if policy and policy['active_version_id'] else None
    client = await one(conn, 'SELECT * FROM ui_extension_clients WHERE owner_id=? AND client_id=?',
                       (str(principal.identity_id), policy['client_id'])) if policy else None
    return {
        'conversation': {'conversation_id': str(conversation_id), 'title': conversation['title'], 'archived': bool(conversation['archived'])},
        'policy': dict(policy) if policy else None,
        'active': version_summary(active) if active else None,
        'client': {**dict(client), 'online': client['last_seen'] >= timestamp(utc_now()-timedelta(seconds=15))} if client else None,
        'versions': [version_summary(row) for row in rows[:50]],
        'events': [dict(row) for row in events[:50]],
        'next_version_before': rows[49]['cursor'] if len(rows) > 50 else None,
        'next_event_before': events[49]['event_id'] if len(events) > 50 else None,
    }


async def decide(service, principal, conversation_id, value):
    async with service.chat.harness.operation, service.database.transaction() as conn:
        policy = await service.policy(conn, principal, conversation_id)
        if not policy or policy['revision'] != value.expected_revision or policy['deployment_revision'] != value.expected_deployment_revision:
            raise Conflict('IDEMPOTENCY_CONFLICT')
        version = await one(conn, '''SELECT * FROM ui_extension_versions
            WHERE version_id=? AND owner_id=? AND conversation_id=? AND plugin_id=?''',
            (str(value.version_id), str(principal.identity_id), str(conversation_id), policy['plugin_id']))
        if not version or version['bundle_digest'] != value.bundle_digest:
            raise AccessDenied('UI_EXTENSION_EVIDENCE_MISMATCH')
        enable = value.action == 'enable'
        if enable:
            service.plugins.require_enabled()
            conversation = await service.chat.conversation(conn, principal, conversation_id)
            if conversation['archived'] or conversation['plan_mode']:
                raise AccessDenied('UI_EXTENSION_POLICY_REQUIRED')
            if (await one(conn, 'SELECT paused FROM system_state WHERE singleton=1'))[0]:
                raise AccessDenied('BUSY')
            if version['status'] != 'accepted' or not json.loads(version['report_json'] or '{}').get('passed'):
                raise AccessDenied('UI_EXTENSION_VERSION_NOT_ACCEPTED')
        # Preserve the originally authorized client and namespace. Never retarget
        # an installation just because management is opened on another device.
        setting = UIExtensionPolicy.model_validate_json(json.dumps({
            'expected_revision': policy['revision'], 'enabled': enable,
            'client_id': policy['client_id'], 'plugin_id': policy['plugin_id'], 'profile': policy['profile'],
        }))
        await service.configure_in_transaction(conn, principal, conversation_id, setting,
                                               active_version_id=version['version_id'] if enable else None)
        return await detail(service, conn, principal, conversation_id)
