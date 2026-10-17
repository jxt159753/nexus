import asyncio
import secrets
import sqlite3
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from nexus.contracts.task import (
    ListDirectoryArguments, ListDirectoryRequest, SystemInfoArguments, SystemInfoRequest,
)
from nexus.core.security.service import SecurityService
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import Database, one, utc_now


async def grants(world):
    for identity in (world.owner_id, world.node_id):
        await world.security.set_grant(identity, "system.info")
        await world.security.set_grant(identity, "filesystem.list", root_id="workspace")


async def task(world, directory=True):
    req = (ListDirectoryRequest(request_id=uuid4(), intent="list_directory", arguments=ListDirectoryArguments(root_id="workspace", limit=10))
           if directory else SystemInfoRequest(request_id=uuid4(), intent="get_system_info", arguments=SystemInfoArguments()))
    return await world.tasks.create(world.owner_id, req, world.node_id)


@pytest.mark.parametrize("role,credential", [("owner", "node"), ("node", "owner"), ("node", "empty"), ("owner", "unknown")])
def test_no_credentials_and_role_impersonation_rejected(world, role, credential):
    async def scenario():
        token = {"node": world.node_token, "owner": world.owner_token, "empty": SecretStr(""), "unknown": SecretStr(secrets.token_urlsafe(32))}[credential]
        with pytest.raises(AccessDenied, match="AUTH_REQUIRED"):
            await world.security.authenticate_token(token, role)
    asyncio.run(scenario())


def test_single_owner_and_unique_independent_credentials(world):
    async def scenario():
        with pytest.raises(sqlite3.IntegrityError):
            await world.security.register_identity(uuid4(), "owner", SecretStr(secrets.token_urlsafe(32)))
        with pytest.raises(sqlite3.IntegrityError):
            await world.security.register_identity(uuid4(), "node", world.owner_token)
    asyncio.run(scenario())


def test_offline_management_audit_does_not_impersonate_node_as_actor(world):
    async def scenario():
        await world.security.set_grant(world.node_id, "system.info")
        async with world.db.connection() as conn:
            rows = await conn.execute_fetchall("SELECT actor_id, subject_id FROM audit_events WHERE kind IN ('identity.registered', 'grant.changed')")
        assert all(row["actor_id"] is None for row in rows)
        assert {row["subject_id"] for row in rows} == {str(world.owner_id), str(world.node_id)}
    asyncio.run(scenario())


@pytest.mark.parametrize("identity", ["node", "owner"])
def test_revocation_and_rotation_invalidate_existing_principal(world, identity):
    async def scenario():
        principal = getattr(world, identity)
        token = getattr(world, identity + "_token")
        identity_id = getattr(world, identity + "_id")
        await world.security.revoke_identity(identity_id)
        with pytest.raises(AccessDenied):
            await world.security.validate_principal(principal)
        with pytest.raises(AccessDenied):
            await world.security.authenticate_token(token, identity)
        new_token = SecretStr(secrets.token_urlsafe(32))
        await world.security.rotate_credential(identity_id, new_token)
        with pytest.raises(AccessDenied):
            await world.security.validate_principal(principal)
        assert (await world.security.authenticate_token(new_token, identity)).identity_id == identity_id
    asyncio.run(scenario())


def test_session_persistence_csrf_expiry_logout_and_secret_redaction(world):
    async def scenario():
        session = await world.security.login(world.owner_token)
        security = SecurityService(Database(world.db.path))
        principal = await security.authenticate_session(session.token, csrf=session.csrf)
        assert principal.role == "owner" and principal.session_id == session.session_id
        with pytest.raises(AccessDenied):
            await security.authenticate_session(session.token, csrf=SecretStr(secrets.token_urlsafe(32)))
        with pytest.raises(AccessDenied):
            await security.authenticate_session(session.token, now=session.expires_at)
        with pytest.raises(AccessDenied):
            await security.authenticate_token(session.token, "owner")
        with pytest.raises(AccessDenied):
            await security.authenticate_session(world.node_token)
        async with world.db.connection() as conn:
            content = []
            for table in ("identities", "sessions", "audit_events"):
                content.extend(str(tuple(row)) for row in await conn.execute_fetchall(f"SELECT * FROM {table}"))
            db_text = " ".join(content)
        for secret in (world.owner_token, world.node_token, session.token, session.csrf):
            assert secret.get_secret_value() not in db_text
            assert secret.get_secret_value() not in repr(session)
            assert secret.get_secret_value() not in repr(principal)
        await security.logout(principal)
        with pytest.raises(AccessDenied):
            await security.validate_principal(principal)
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["revoke", "rotate"])
def test_owner_credential_changes_revoke_browser_sessions(world, operation):
    async def scenario():
        session = await world.security.login(world.owner_token)
        principal = await world.security.authenticate_session(session.token)
        if operation == "revoke":
            await world.security.revoke_identity(world.owner_id)
        else:
            await world.security.rotate_credential(world.owner_id, SecretStr(secrets.token_urlsafe(32)))
        with pytest.raises(AccessDenied):
            await world.security.validate_principal(principal)
    asyncio.run(scenario())


def test_active_browser_sessions_are_bounded(world):
    async def scenario():
        for _ in range(10):
            await world.security.login(world.owner_token)
        with pytest.raises(AccessDenied, match="BUSY"):
            await world.security.login(world.owner_token)
    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["all_grants", "owner_grant", "node_grant", "registered_tool", "local_tool", "registered_root", "local_root", "wrong_node"])
def test_capability_intersection_deny_cannot_be_approved(world, missing):
    async def scenario():
        await grants(world)
        scope = world.scope
        if missing == "all_grants":
            for identity in (world.owner_id, world.node_id):
                await world.security.set_grant(identity, "filesystem.list", root_id="workspace", enabled=False)
        elif missing in {"owner_grant", "node_grant"}:
            await world.security.set_grant(world.owner_id if missing == "owner_grant" else world.node_id, "filesystem.list", root_id="workspace", enabled=False)
        elif missing == "wrong_node":
            scope = replace(scope, node_id=uuid4())
        else:
            field = {"registered_tool": "tools", "local_tool": "local_tools", "registered_root": "roots", "local_root": "local_roots"}[missing]
            scope = replace(scope, **{field: frozenset()})
        pending = await task(world)
        with pytest.raises(AccessDenied, match="POLICY_DENIED"):
            await world.security.prepare(pending.task_id, world.owner, scope)
        assert (await world.tasks.get(pending.task_id, world.owner_id)).status == "queued"
        async with world.db.connection() as conn:
            assert (await one(conn, "SELECT count(*) FROM approvals"))[0] == 0
    asyncio.run(scenario())


def test_system_info_requires_explicit_grants_but_no_manual_approval(world):
    async def scenario():
        pending = await task(world, directory=False)
        with pytest.raises(AccessDenied):
            await world.security.prepare(pending.task_id, world.owner, world.scope)
        await grants(world)
        verdict = await world.security.prepare(pending.task_id, world.owner, world.scope)
        assert verdict.mode == "allow" and verdict.approval_id is None
        assert (await world.tasks.get(pending.task_id, world.owner_id)).status == "ready"
        assert (await world.security.require_authorized(pending.task_id, world.owner, world.scope)).mode == "allow"
    asyncio.run(scenario())


def test_approval_is_persistent_idempotent_and_task_bound(world):
    async def scenario():
        await grants(world)
        first = await task(world)
        verdict = await world.security.prepare(first.task_id, world.owner, world.scope)
        assert verdict.mode == "approval_required"
        assert verdict == await world.security.prepare(first.task_id, world.owner, world.scope)
        assert verdict.expires_at <= first.expires_at
        assert (await world.tasks.get(first.task_id, world.owner_id)).status == "awaiting_approval"
        with pytest.raises(AccessDenied):
            await world.security.require_authorized(first.task_id, world.owner, world.scope)
        security = SecurityService(Database(world.db.path))
        approved = await security.decide(verdict.approval_id, world.owner, "approve", world.scope)
        assert approved.status == "ready"
        assert approved == await security.decide(verdict.approval_id, world.owner, "approve", world.scope)
        assert (await security.require_authorized(first.task_id, world.owner, world.scope)).approval_id == verdict.approval_id
        second = await task(world)
        await security.prepare(second.task_id, world.owner, world.scope)
        # Deciding the first approval only ever refers to the first task.
        assert (await security.decide(verdict.approval_id, world.owner, "approve", world.scope)).task_id == first.task_id
        with pytest.raises(AccessDenied):
            await security.require_authorized(second.task_id, world.owner, world.scope)
    asyncio.run(scenario())


@pytest.mark.parametrize("binding", ["node_id", "arguments_digest", "task_id", "owner_id", "tool", "tool_version", "policy_version", "scope_digest"])
def test_wrong_approval_binding_cannot_unlock_task(world, binding):
    async def scenario():
        await grants(world)
        pending = await task(world)
        verdict = await world.security.prepare(pending.task_id, world.owner, world.scope)
        if binding == "task_id":
            value = str((await task(world)).task_id)
        else:
            value = {"node_id": str(world.owner_id), "arguments_digest": "0" * 64, "owner_id": str(world.node_id), "tool": "shell.execute", "tool_version": 2, "policy_version": 999, "scope_digest": "0" * 64}.get(binding)
        async with world.db.transaction() as conn:
            await conn.execute(f"UPDATE approvals SET {binding}=? WHERE approval_id=?", (value, str(verdict.approval_id)))
        with pytest.raises(AccessDenied):
            await world.security.decide(verdict.approval_id, world.owner, "approve", world.scope)
        assert (await world.tasks.get(pending.task_id, world.owner_id)).status == "awaiting_approval"
    asyncio.run(scenario())


def test_approval_expiration_refresh_and_task_deadline(world):
    async def scenario():
        await grants(world)
        pending = await task(world)
        now = utc_now()
        verdict = await world.security.prepare(pending.task_id, world.owner, world.scope, now=now)
        assert verdict.expires_at == now + timedelta(minutes=2)
        with pytest.raises(AccessDenied, match="APPROVAL_EXPIRED"):
            await world.security.decide(verdict.approval_id, world.owner, "approve", world.scope, now=verdict.expires_at)
        renewed = await world.security.prepare(pending.task_id, world.owner, world.scope, now=now + timedelta(minutes=4))
        assert renewed.approval_id != verdict.approval_id
        assert renewed.expires_at == pending.expires_at
        await world.security.decide(renewed.approval_id, world.owner, "approve", world.scope, now=now + timedelta(minutes=4))
        with pytest.raises(AccessDenied):
            await world.security.require_authorized(pending.task_id, world.owner, world.scope, now=pending.expires_at)
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["grant", "scope", "node_revoked", "resource_version"])
def test_approved_action_rechecked_after_policy_resource_or_node_change(world, change):
    async def scenario():
        await grants(world)
        pending = await task(world)
        verdict = await world.security.prepare(pending.task_id, world.owner, world.scope)
        await world.security.decide(verdict.approval_id, world.owner, "approve", world.scope)
        scope = world.scope
        if change == "grant":
            await world.security.set_grant(world.node_id, "filesystem.list", root_id="workspace", enabled=False)
            await world.security.set_grant(world.node_id, "filesystem.list", root_id="workspace")
        elif change == "scope":
            scope = replace(scope, tools=frozenset({"filesystem.list@1"}))
        elif change == "resource_version":
            scope = replace(scope, resource_version=2)
        else:
            await world.security.revoke_identity(world.node_id)
        with pytest.raises(AccessDenied):
            await world.security.require_authorized(pending.task_id, world.owner, scope)
        with pytest.raises(AccessDenied):
            await world.security.decide(verdict.approval_id, world.owner, "approve", scope)
        if change != "node_revoked":
            new = await world.security.prepare(pending.task_id, world.owner, scope)
            assert new.approval_id != verdict.approval_id
            assert (await world.tasks.get(pending.task_id, world.owner_id)).status == "awaiting_approval"
    asyncio.run(scenario())


def test_node_cannot_approve_or_change_task_and_rejection_is_final(world):
    async def scenario():
        await grants(world)
        pending = await task(world)
        verdict = await world.security.prepare(pending.task_id, world.owner, world.scope)
        with pytest.raises(AccessDenied):
            await world.security.decide(verdict.approval_id, world.node, "approve", world.scope)
        with pytest.raises(AccessDenied):
            await world.security.prepare(pending.task_id, world.node, world.scope)
        rejected = await world.security.decide(verdict.approval_id, world.owner, "reject", world.scope)
        assert rejected.status == "failed"
        with pytest.raises(AccessDenied):
            await world.security.decide(verdict.approval_id, world.owner, "approve", world.scope)
        with pytest.raises(AccessDenied):
            await world.security.prepare(pending.task_id, world.owner, world.scope)
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["prepare", "decide"])
def test_approval_state_and_audit_rollback_together(world, phase):
    async def scenario():
        await grants(world)
        pending = await task(world)
        verdict = None
        if phase == "decide":
            verdict = await world.security.prepare(pending.task_id, world.owner, world.scope)
        before = await world.tasks.get(pending.task_id, world.owner_id)
        async with world.db.transaction() as conn:
            await conn.execute("CREATE TRIGGER fail_approval_audit BEFORE INSERT ON audit_events WHEN NEW.kind LIKE 'approval.%' BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="audit failure"):
            if phase == "prepare":
                await world.security.prepare(pending.task_id, world.owner, world.scope)
            else:
                await world.security.decide(verdict.approval_id, world.owner, "approve", world.scope)
        assert await world.tasks.get(pending.task_id, world.owner_id) == before
        async with world.db.connection() as conn:
            rows = await conn.execute_fetchall("SELECT decision FROM approvals")
            assert [row[0] for row in rows] == ([] if phase == "prepare" else [None])
    asyncio.run(scenario())


def test_concurrent_approval_decisions_have_one_persistent_winner(world):
    async def scenario():
        await grants(world)
        pending = await task(world)
        verdict = await world.security.prepare(pending.task_id, world.owner, world.scope)
        results = await asyncio.gather(*(
            world.security.decide(verdict.approval_id, world.owner, choice, world.scope)
            for choice in ("approve", "reject")
        ), return_exceptions=True)
        assert sum(isinstance(r, AccessDenied) for r in results) == 1
        final = await world.tasks.get(pending.task_id, world.owner_id)
        assert final.status in {"ready", "failed"} and final.revision == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("tool,root", [("shell.execute", ""), ("filesystem.list", "C:\\"), ("filesystem.list", ""), ("system.info", "workspace")])
def test_forbidden_tools_and_arbitrary_resources_have_no_grant_path(world, tool, root):
    with pytest.raises(ValueError, match="unsupported grant"):
        asyncio.run(world.security.set_grant(world.owner_id, tool, root_id=root))
