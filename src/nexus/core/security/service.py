"""Offline enrollment plus per-operation identity, scope and approval checks."""

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import aiosqlite
from pydantic import SecretStr

from nexus.core.security.types import (
    AccessDenied, Authorization, BrowserSession, NodeScope, Principal,
)
from nexus.core.storage.database import Database, audit, one, timestamp, utc_now
from nexus.contracts.tools import TOOL_ARGUMENT_MODELS
from nexus.core.storage.tasks import (
    TaskRecord, canonical, load_task, transition,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def token_hash(token: SecretStr) -> str:
    value = token.get_secret_value()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", value):
        raise AccessDenied("AUTH_REQUIRED")
    return digest(value)


def scope_digest(scope: NodeScope) -> str:
    return digest(canonical({
        "node_id": str(scope.node_id),
        "resource_version": scope.resource_version,
        "tools": sorted(scope.tools), "roots": sorted(scope.roots),
        "local_tools": sorted(scope.local_tools), "local_roots": sorted(scope.local_roots),
        **({"command_digests": scope.command_digests} if scope.command_digests else {}),
    }))


def arguments_digest(task: TaskRecord) -> str:
    return digest(canonical(task.action.arguments.model_dump(mode="json")))


async def assert_principal(conn: aiosqlite.Connection, principal: Principal, now: datetime) -> None:
    identity = await one(conn, "SELECT * FROM identities WHERE identity_id=?", (str(principal.identity_id),))
    if identity is None or identity["revoked_at"] is not None or identity["role"] != principal.role:
        raise AccessDenied("AUTH_REQUIRED")
    if principal.session_id is None:
        if not secrets.compare_digest(identity["token_hash"], principal.credential_hash):
            raise AccessDenied("AUTH_REQUIRED")
    else:
        session = await one(conn, "SELECT * FROM sessions WHERE session_id=? AND owner_id=?", (str(principal.session_id), str(principal.identity_id)))
        if session is None or session["revoked_at"] is not None or session["expires_at"] <= timestamp(now) or not secrets.compare_digest(session["token_hash"], principal.credential_hash):
            raise AccessDenied("AUTH_REQUIRED")


async def assert_owner(conn: aiosqlite.Connection, principal: Principal, now: datetime) -> None:
    await assert_principal(conn, principal, now)
    if principal.role != "owner":
        raise AccessDenied()


class SecurityService:
    def __init__(self, database: Database):
        self.database = database

    async def register_identity(self, identity_id: UUID, role: Literal["owner", "node"], token: SecretStr) -> None:
        """Local management only; not a self-registration network API."""
        hashed = token_hash(token)
        if role not in {"owner", "node"}:
            raise ValueError("invalid identity role")
        async with self.database.transaction() as conn:
            await conn.execute("INSERT INTO identities VALUES (?, ?, ?, NULL, ?)", (str(identity_id), role, hashed, timestamp(utc_now())))
            await audit(conn, None, None, "identity.registered", utc_now(), subject_id=str(identity_id))

    async def revoke_identity(self, identity_id: UUID) -> None:
        async with self.database.transaction() as conn:
            now = utc_now()
            async with conn.execute("UPDATE identities SET revoked_at=? WHERE identity_id=?", (timestamp(now), str(identity_id))) as cursor:
                if cursor.rowcount != 1:
                    raise LookupError("identity not found")
            await conn.execute("UPDATE sessions SET revoked_at=? WHERE owner_id=?", (timestamp(now), str(identity_id)))
            await conn.execute("UPDATE policy_state SET version=version+1")
            await audit(conn, None, None, "identity.revoked", now, subject_id=str(identity_id))

    async def rotate_credential(self, identity_id: UUID, token: SecretStr) -> None:
        hashed = token_hash(token)
        async with self.database.transaction() as conn:
            now = utc_now()
            async with conn.execute("UPDATE identities SET token_hash=?, revoked_at=NULL WHERE identity_id=?", (hashed, str(identity_id))) as cursor:
                if cursor.rowcount != 1:
                    raise LookupError("identity not found")
            await conn.execute("UPDATE sessions SET revoked_at=? WHERE owner_id=?", (timestamp(now), str(identity_id)))
            await conn.execute("UPDATE policy_state SET version=version+1")
            await audit(conn, None, None, "identity.rotated", now, subject_id=str(identity_id))

    async def authenticate_token(self, token: SecretStr, role: Literal["owner", "node"]) -> Principal:
        hashed = token_hash(token)
        async with self.database.transaction(write=False) as conn:
            row = await one(conn, "SELECT * FROM identities WHERE token_hash=? AND role=? AND revoked_at IS NULL", (hashed, role))
            if row is None:
                raise AccessDenied("AUTH_REQUIRED")
            return Principal(UUID(row["identity_id"]), role, hashed)

    async def validate_principal(self, principal: Principal, *, now: datetime | None = None) -> None:
        async with self.database.transaction(write=False) as conn:
            await assert_principal(conn, principal, now or utc_now())

    async def connection_scope(self, principal: Principal, owner_id: UUID, scope: NodeScope) -> NodeScope:
        """Intersect trusted registration with both persisted identities' grants."""
        async with self.database.transaction(write=False) as conn:
            await assert_principal(conn, principal, utc_now())
            if principal.role != "node" or scope.node_id != principal.identity_id:
                raise AccessDenied()
            owner = await one(conn, "SELECT role, revoked_at FROM identities WHERE identity_id=?", (str(owner_id),))
            if owner is None or owner["role"] != "owner" or owner["revoked_at"] is not None:
                raise AccessDenied()
            granted = []
            for identity in (owner_id, principal.identity_id):
                rows = await conn.execute_fetchall("SELECT tool, tool_version, root_id FROM grants WHERE identity_id=?", (str(identity),))
                granted.append({(f"{row['tool']}@{row['tool_version']}", row['root_id']) for row in rows})
            shared = granted[0] & granted[1]
            tools = frozenset(key for key in scope.tools & scope.local_tools if (
                key == "system.info@1" and (key, "") in shared
                or key != "system.info@1" and (key, "workspace") in shared
                and "workspace" in scope.roots & scope.local_roots
            ))
            roots = frozenset({"workspace"}) if tools-{"system.info@1"} else frozenset()
            return NodeScope(scope.node_id, tools, roots, scope.local_tools, scope.local_roots, scope.resource_version, scope.command_digests)

    async def login(self, token: SecretStr, *, now: datetime | None = None) -> BrowserSession:
        now = now or utc_now()
        principal = await self.authenticate_token(token, "owner")
        session_id = uuid4()
        session_token = SecretStr(secrets.token_urlsafe(32))
        csrf = SecretStr(digest("nexus-csrf-v1:" + session_token.get_secret_value()))
        expires = now + timedelta(hours=8)
        async with self.database.transaction() as conn:
            await assert_owner(conn, principal, now)
            active = await one(conn, "SELECT count(*) AS n FROM sessions WHERE owner_id=? AND revoked_at IS NULL AND expires_at>?", (str(principal.identity_id), timestamp(now)))
            if active["n"] >= 10:
                raise AccessDenied("BUSY")
            await conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, NULL)", (
                str(session_id), str(principal.identity_id), token_hash(session_token), token_hash(csrf), timestamp(expires),
            ))
            await audit(conn, str(principal.identity_id), None, "session.created", now)
        return BrowserSession(session_id, session_token, csrf, expires)

    async def session_info(self, token: SecretStr) -> dict:
        principal = await self.authenticate_session(token)
        csrf = digest("nexus-csrf-v1:" + token.get_secret_value())
        async with self.database.transaction(write=False) as conn:
            await assert_owner(conn, principal, utc_now())
            row = await one(conn, "SELECT csrf_hash,expires_at FROM sessions WHERE session_id=?", (str(principal.session_id),))
            if not secrets.compare_digest(row["csrf_hash"], digest(csrf)):
                raise AccessDenied("AUTH_REQUIRED")  # Pre-S6 sessions require a new login.
            return {"csrf_token": csrf, "expires_at": row["expires_at"], "owner_id": str(principal.identity_id)}

    async def authenticate_session(self, token: SecretStr, *, csrf: SecretStr | None = None, now: datetime | None = None) -> Principal:
        now = now or utc_now()
        hashed = token_hash(token)
        async with self.database.transaction(write=False) as conn:
            row = await one(conn, "SELECT * FROM sessions WHERE token_hash=?", (hashed,))
            if row is None:
                raise AccessDenied("AUTH_REQUIRED")
            principal = Principal(UUID(row["owner_id"]), "owner", hashed, UUID(row["session_id"]))
            await assert_owner(conn, principal, now)
            if csrf is not None and not secrets.compare_digest(row["csrf_hash"], token_hash(csrf)):
                raise AccessDenied()
            return principal

    async def logout(self, principal: Principal) -> None:
        async with self.database.transaction() as conn:
            await assert_owner(conn, principal, utc_now())
            if principal.session_id is None:
                raise AccessDenied()
            await conn.execute("UPDATE sessions SET revoked_at=? WHERE session_id=?", (timestamp(utc_now()), str(principal.session_id)))
            await audit(conn, str(principal.identity_id), None, "session.revoked", utc_now())

    async def set_grant(self, identity_id: UUID, tool: str, *, root_id: str = "", enabled: bool = True) -> None:
        """Explicit offline configuration, separate for owner and each Node."""
        if (f"{tool}@1" not in TOOL_ARGUMENT_MODELS or root_id != ("" if tool == "system.info" else "workspace")) or type(enabled) is not bool:
            raise ValueError("unsupported grant")
        async with self.database.transaction() as conn:
            row = await one(conn, "SELECT revoked_at FROM identities WHERE identity_id=?", (str(identity_id),))
            if row is None or row[0] is not None:
                raise AccessDenied()
            if enabled:
                async with conn.execute("INSERT OR IGNORE INTO grants VALUES (?, ?, ?, 1, ?)", (str(uuid4()), str(identity_id), tool, root_id)) as cursor:
                    changed = cursor.rowcount
            else:
                async with conn.execute("DELETE FROM grants WHERE identity_id=? AND tool=? AND root_id=?", (str(identity_id), tool, root_id)) as cursor:
                    changed = cursor.rowcount
            if changed:
                await conn.execute("UPDATE policy_state SET version=version+1")
                await audit(conn, None, None, "grant.changed", utc_now(), subject_id=str(identity_id))

    async def _eligible(self, conn: aiosqlite.Connection, task: TaskRecord, principal: Principal, scope: NodeScope, now: datetime) -> int:
        await assert_owner(conn, principal, now)
        if task.owner_id != principal.identity_id:
            raise AccessDenied()
        return await self._policy_eligible(conn, task, scope, now)

    async def _policy_eligible(self, conn: aiosqlite.Connection, task: TaskRecord, scope: NodeScope, now: datetime, *, require_unexpired: bool = True) -> int:
        """Recheck durable identities and grants independently of browser presence."""
        owner = await one(conn, "SELECT role, revoked_at FROM identities WHERE identity_id=?", (str(task.owner_id),))
        if owner is None or owner["role"] != "owner" or owner["revoked_at"] is not None:
            raise AccessDenied("POLICY_DENIED")
        if scope.node_id != task.node_id or (require_unexpired and now >= task.expires_at):
            raise AccessDenied("POLICY_DENIED")
        node = await one(conn, "SELECT role, revoked_at FROM identities WHERE identity_id=?", (str(task.node_id),))
        if node is None or node["role"] != "node" or node["revoked_at"] is not None:
            raise AccessDenied("POLICY_DENIED")
        key = f"{task.action.tool}@{task.action.tool_version}"
        if key not in TOOL_ARGUMENT_MODELS or key not in scope.tools or key not in scope.local_tools:
            raise AccessDenied("POLICY_DENIED")
        if task.action.tool == "command.run" and scope.command_digests.get(task.action.arguments.command_id) != task.action.arguments.template_digest:
            raise AccessDenied("POLICY_DENIED")
        root = getattr(task.action.arguments, "root_id", "")
        if root and (root not in scope.roots or root not in scope.local_roots):
            raise AccessDenied("POLICY_DENIED")
        for identity in (task.owner_id, task.node_id):
            row = await one(conn, "SELECT grant_id FROM grants WHERE identity_id=? AND tool=? AND tool_version=? AND root_id=?", (str(identity), task.action.tool, task.action.tool_version, root))
            if row is None:
                raise AccessDenied("POLICY_DENIED")
        return (await one(conn, "SELECT version FROM policy_state WHERE singleton=1"))[0]

    @staticmethod
    def _matches(approval: aiosqlite.Row, task: TaskRecord, version: int, scope: NodeScope) -> bool:
        return (
            approval["task_id"] == str(task.task_id) and approval["owner_id"] == str(task.owner_id)
            and approval["node_id"] == str(task.node_id) and approval["tool"] == task.action.tool
            and approval["tool_version"] == task.action.tool_version
            and approval["arguments_digest"] == arguments_digest(task)
            and approval["policy_version"] == version and approval["scope_digest"] == scope_digest(scope)
            and approval["expires_at"] <= timestamp(task.expires_at)
        )

    async def prepare(self, task_id: UUID, principal: Principal, scope: NodeScope, *, now: datetime | None = None) -> Authorization:
        now = now or utc_now()
        async with self.database.transaction() as conn:
            task = await load_task(conn, task_id)
            version = await self._eligible(conn, task, principal, scope, now)
            return await self._prepare(conn, task, scope, version, now)

    async def prepare_pending(self, task_id: UUID, node: Principal, scope: NodeScope) -> Authorization:
        now = utc_now()
        async with self.database.transaction() as conn:
            await assert_principal(conn, node, now)
            task = await load_task(conn, task_id)
            if node.role != "node" or node.identity_id != task.node_id:
                raise AccessDenied()
            version = await self._policy_eligible(conn, task, scope, now)
            return await self._prepare(conn, task, scope, version, now)

    async def _prepare(self, conn: aiosqlite.Connection, task: TaskRecord, scope: NodeScope, version: int, now: datetime) -> Authorization:
        from nexus.core.security.work import check_task
        work_allowed = await check_task(conn, task, scope, now)
        if task.status == "recovering" and not task.cancel_requested:
            old = await one(conn, "SELECT execution_stopped,phase FROM task_attempts WHERE attempt_id=?", (str(task.current_attempt_id),))
            if old is not None and old["execution_stopped"] and old["phase"] == "completed":
                task = await transition(conn, task, "ready", task.owner_id, now, execution=True)
        if task.status not in {"queued", "awaiting_approval", "ready"}:
            raise AccessDenied("POLICY_DENIED")
        if task.action.tool == "system.info":
            if task.status != "ready":
                await transition(conn, task, "ready", task.owner_id, now)
            return Authorization("allow", version)
        row = await one(conn, "SELECT * FROM approvals WHERE task_id=? AND policy_version=? AND scope_digest=? AND expires_at>? ORDER BY rowid DESC LIMIT 1", (str(task.task_id), version, scope_digest(scope), timestamp(now)))
        if row is not None and self._matches(row, task, version, scope):
            if row["decision"] == "reject":
                raise AccessDenied("POLICY_DENIED")
            wanted = "ready" if row["decision"] == "approve" else "awaiting_approval"
            if task.status != wanted:
                await transition(conn, task, wanted, task.owner_id, now)
            return Authorization("allow" if row["decision"] == "approve" else "approval_required", version, UUID(row["approval_id"]), datetime.fromisoformat(row["expires_at"]))
        approval_id = uuid4()
        expires = min(task.expires_at, now + timedelta(minutes=2))
        await conn.execute("INSERT INTO approvals(approval_id, task_id, owner_id, node_id, tool, tool_version, arguments_digest, policy_version, scope_digest, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            str(approval_id), str(task.task_id), str(task.owner_id), str(task.node_id), task.action.tool,
            task.action.tool_version, arguments_digest(task), version, scope_digest(scope), timestamp(expires),
        ))
        if work_allowed:
            await conn.execute("UPDATE approvals SET decision='approve',decided_by=?,decided_at=? WHERE approval_id=?",(str(task.owner_id),timestamp(now),str(approval_id)))
            if task.status != "ready":
                await transition(conn,task,"ready",task.owner_id,now)
            await audit(conn,str(task.owner_id),str(task.task_id),"approval.work_grant",now)
            return Authorization("allow",version,approval_id,expires)
        if task.status != "awaiting_approval":
            await transition(conn, task, "awaiting_approval", task.owner_id, now)
        else:
            await conn.execute("UPDATE tasks SET revision=revision+1 WHERE task_id=?", (str(task.task_id),))
            await conn.execute("INSERT INTO task_events(task_id, revision, kind, created_at) VALUES (?, ?, 'approval.required', ?)", (str(task.task_id), task.revision + 1, timestamp(now)))
        await audit(conn, str(task.owner_id), str(task.task_id), "approval.required", now)
        return Authorization("approval_required", version, approval_id, expires)

    async def decide(self, approval_id: UUID, principal: Principal, decision: Literal["approve", "reject"], scope: NodeScope, *, now: datetime | None = None) -> TaskRecord:
        if decision not in {"approve", "reject"}:
            raise ValueError("invalid approval decision")
        now = now or utc_now()
        async with self.database.transaction() as conn:
            await assert_owner(conn, principal, now)
            approval = await one(conn, "SELECT * FROM approvals WHERE approval_id=?", (str(approval_id),))
            if approval is None:
                raise AccessDenied()
            task = await load_task(conn, UUID(approval["task_id"]))
            version = await self._eligible(conn, task, principal, scope, now)
            if not self._matches(approval, task, version, scope):
                raise AccessDenied("POLICY_DENIED")
            if approval["expires_at"] <= timestamp(now):
                raise AccessDenied("APPROVAL_EXPIRED")
            if approval["decision"] is not None:
                if approval["decision"] == decision:
                    return task
                raise AccessDenied()
            if task.status != "awaiting_approval":
                raise AccessDenied("POLICY_DENIED")
            await conn.execute("UPDATE approvals SET decision=?, decided_by=?, decided_at=? WHERE approval_id=?", (decision, str(principal.identity_id), timestamp(now), str(approval_id)))
            task = await transition(conn, task, "ready" if decision == "approve" else "failed", principal.identity_id, now)
            await audit(conn, str(principal.identity_id), str(task.task_id), f"approval.{decision}", now)
            return task

    async def require_authorized(self, task_id: UUID, principal: Principal, scope: NodeScope, *, now: datetime | None = None) -> Authorization:
        """A check, not a transferable execution token."""
        now = now or utc_now()
        async with self.database.transaction(write=False) as conn:
            task = await load_task(conn, task_id)
            version = await self._eligible(conn, task, principal, scope, now)
            return await self._authorized(conn, task, scope, version, now)

    async def authorize_dispatch(self, conn: aiosqlite.Connection, task: TaskRecord, node: Principal, scope: NodeScope, now: datetime) -> Authorization:
        """Harness only: call inside the transaction that creates grant/attempt."""
        await assert_principal(conn, node, now)
        if node.role != "node" or node.identity_id != task.node_id:
            raise AccessDenied()
        version = await self._policy_eligible(conn, task, scope, now)
        return await self._authorized(conn, task, scope, version, now)

    async def _authorized(self, conn: aiosqlite.Connection, task: TaskRecord, scope: NodeScope, version: int, now: datetime) -> Authorization:
        from nexus.core.security.work import check_task
        await check_task(conn, task, scope, now)
        if task.status != "ready":
            raise AccessDenied("POLICY_DENIED")
        if task.action.tool == "system.info":
            return Authorization("allow", version)
        row = await one(conn, "SELECT * FROM approvals WHERE task_id=? AND policy_version=? AND scope_digest=? AND decision='approve' ORDER BY rowid DESC LIMIT 1", (str(task.task_id), version, scope_digest(scope)))
        if row is None or not self._matches(row, task, version, scope):
            raise AccessDenied("POLICY_DENIED")
        if row["expires_at"] <= timestamp(now):
            raise AccessDenied("APPROVAL_EXPIRED")
        return Authorization("allow", version, UUID(row["approval_id"]), datetime.fromisoformat(row["expires_at"]))
