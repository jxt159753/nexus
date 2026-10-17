"""Authority, durable dispatch, hostile evidence and verifier crash boundaries."""

import asyncio
import json
import secrets
import sqlite3
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.messages import CommandReceipt
from nexus.contracts.task import ListDirectoryArguments, ListDirectoryRequest
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.core.storage.execution import ExecutionRepository, RESULT
from test_storage import request


async def ready(world, directory=False, *, approve=True):
    tool = "filesystem.list" if directory else "system.info"
    for identity in (world.owner_id, world.node_id):
        await world.security.set_grant(identity, tool, root_id="workspace" if directory else "")
    req = ListDirectoryRequest(request_id=uuid4(), intent="list_directory", arguments=ListDirectoryArguments(root_id="workspace", limit=2)) if directory else request()
    task = await world.tasks.create(world.owner_id, req, world.node_id)
    auth = await world.security.prepare(task.task_id, world.owner, world.scope)
    if directory and approve:
        await world.security.decide(auth.approval_id, world.owner, "approve", world.scope)
    return await world.tasks.get(task.task_id, world.owner_id)


async def dispatched(world, directory=False):
    task = await ready(world, directory)
    repo = ExecutionRepository(world.db, world.security, 32768)
    boot_id = uuid4()
    command = await repo.dispatch(task.task_id, world.node, world.scope, uuid4(), boot_id, 10)
    return repo, command, boot_id


def result_for(command, **changes):
    values = command.model_dump(mode="json", include={"session_id", "task_id", "attempt_id", "tool", "tool_version", "command_digest"})
    values.update(observed_at=utc_now().isoformat(), outcome="succeeded", result=(
        {"root_id": "workspace", "entries": [{"name": "docs", "kind": "directory"}], "truncated": False}
        if command.tool == "filesystem.list" else
        {"os_name": "Windows", "os_version": "test", "architecture": "test", "python_version": "test"}
    ))
    values.update(changes)
    return RESULT.validate_json(json.dumps(values))


def test_result_without_acceptance_persists_then_verifies_after_restart(world):
    async def scenario():
        repo, command, _ = await dispatched(world)
        result = result_for(command)
        assert await repo.store_result(world.node, result) == "stored"
        verifying = await world.tasks.get(command.task_id, world.owner_id)
        assert verifying.status == "verifying" and verifying.result is None
        assert verifying.execution_outcome == "unknown"
        async with world.db.connection() as conn:
            assert (await one(conn, "SELECT result_json FROM task_attempts"))[0]
        await ExecutionRepository(world.db, world.security, 32768).resume_verification()
        succeeded = await world.tasks.get(command.task_id, world.owner_id)
        assert succeeded.status == "succeeded" and succeeded.execution_outcome == "known"
        assert succeeded.result.os_name == "Windows"
        assert succeeded.verification == "node_report_schema_checked"
        assert await repo.store_result(world.node, result.model_copy(update={"session_id": uuid4()})) == "already_settled"
        await repo.verify(command.attempt_id)
        assert await world.tasks.get(command.task_id, world.owner_id) == succeeded
        detail = await repo.detail(command.task_id, world.owner_id)
        assert [e["kind"] for e in detail["events"]] == ["queued", "ready", "dispatching", "verifying", "succeeded"]
        assert len(detail["attempts"]) == 1 and detail["attempts"][0]["observed_at"]
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["task", "attempt", "digest", "tool", "version", "node", "future", "old", "inactive_attempt"])
def test_mismatched_results_cannot_mutate_active_attempt(world, kind):
    async def scenario():
        repo, command, _ = await dispatched(world)
        result, principal = result_for(command), world.node
        if kind in {"task", "attempt", "digest"}:
            result = result.model_copy(update={f"{kind}_id" if kind != "digest" else "command_digest": uuid4() if kind != "digest" else "f" * 64})
        elif kind == "tool":
            values = result.model_dump(mode="json")
            values.update(tool="filesystem.list", result={"root_id": "workspace", "entries": [], "truncated": False})
            result = RESULT.validate_json(json.dumps(values))
        elif kind == "version":
            result = result.model_copy(update={"tool_version": 2})
        elif kind == "node":
            other = uuid4()
            token = SecretStr(secrets.token_urlsafe(32))
            await world.security.register_identity(other, "node", token)
            principal = await world.security.authenticate_token(token, "node")
        elif kind in {"future", "old"}:
            result = result.model_copy(update={"observed_at": utc_now() + timedelta(hours=1 if kind == "future" else -1)})
        else:
            async with world.db.transaction() as conn:
                await conn.execute("UPDATE tasks SET current_attempt_id=NULL")
        before = await world.tasks.get(command.task_id, world.owner_id)
        with pytest.raises((ProtocolError, ValueError)):
            await repo.store_result(principal, result)
        assert await world.tasks.get(command.task_id, world.owner_id) == before
        async with world.db.connection() as conn:
            assert (await one(conn, "SELECT result_json FROM task_attempts"))[0] is None
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["count", "path", "duplicate", "bytes", "failure"])
def test_matching_evidence_still_requires_tool_validation(world, kind):
    async def scenario():
        repo, command, _ = await dispatched(world, directory=True)
        output = {"root_id": "workspace", "entries": [], "truncated": False}
        if kind == "count":
            output["entries"] = [{"name": str(i), "kind": "file"} for i in range(3)]
        elif kind == "path":
            output["entries"] = [{"name": "../secret", "kind": "file"}]
        elif kind == "duplicate":
            output["entries"] = [{"name": "same", "kind": "file"}] * 2
        elif kind == "bytes":
            repo.max_result_bytes = 128
            output["entries"] = [{"name": "a" * 200, "kind": "file"}]
        if kind == "failure":
            result = result_for(command, outcome="failed", result=None, error={"code": "TOOL_TIMEOUT", "message": "Timed out", "retryable": True})
        else:
            result = result_for(command, result=output)
        if kind == "bytes":
            with pytest.raises(ProtocolError):
                await repo.store_result(world.node, result)
            assert (await world.tasks.get(command.task_id, world.owner_id)).status == "dispatching"
            return
        await repo.store_result(world.node, result)
        await repo.verify(command.attempt_id)
        task = await world.tasks.get(command.task_id, world.owner_id)
        assert task.status == "failed" and task.result is None and task.verification is None
        assert task.error.code == (ErrorCode.TOOL_TIMEOUT if kind == "failure" else ErrorCode.RESULT_INVALID)
        assert task.execution_outcome == "known"
    asyncio.run(scenario())


def test_receipts_boot_and_terminal_duplicates_do_not_override_evidence(world):
    async def scenario():
        repo, command, boot = await dispatched(world)
        receipt = CommandReceipt(session_id=command.session_id, task_id=command.task_id,
            attempt_id=command.attempt_id, command_digest=command.command_digest, boot_id=boot)
        with pytest.raises(ProtocolError):
            await repo.receipt(world.node, uuid4(), receipt)
        await repo.receipt(world.node, boot, receipt)
        task = await world.tasks.get(command.task_id, world.owner_id)
        assert task.status == "running"
        await repo.receipt(world.node, boot, receipt)
        assert await world.tasks.get(command.task_id, world.owner_id) == task
        result = result_for(command)
        await repo.store_result(world.node, result)
        await repo.verify(command.attempt_id)
        finished = await world.tasks.get(command.task_id, world.owner_id)
        with pytest.raises(ProtocolError):
            await repo.store_result(world.node, result_for(command, observed_at=(result.observed_at + timedelta(seconds=1)).isoformat()))
        await repo.receipt(world.node, boot, receipt)
        assert await world.tasks.get(command.task_id, world.owner_id) == finished
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["owner_grant", "node_grant", "policy", "scope", "approval_expired", "owner_revoked", "node_revoked", "unapproved"])
def test_dispatch_rechecks_current_authority_in_its_transaction(world, kind):
    async def scenario():
        task = await ready(world, directory=True, approve=kind != "unapproved")
        scope = world.scope
        if kind in {"owner_grant", "node_grant"}:
            await world.security.set_grant(world.owner_id if kind == "owner_grant" else world.node_id, "filesystem.list", root_id="workspace", enabled=False)
        elif kind == "policy":
            await world.security.set_grant(world.owner_id, "system.info")
        elif kind == "scope":
            from dataclasses import replace
            scope = replace(scope, resource_version=2)
        elif kind == "approval_expired":
            async with world.db.transaction() as conn:
                await conn.execute("UPDATE approvals SET expires_at=?", (timestamp(utc_now() - timedelta(seconds=1)),))
        elif kind in {"owner_revoked", "node_revoked"}:
            await world.security.revoke_identity(world.owner_id if kind == "owner_revoked" else world.node_id)
        repo = ExecutionRepository(world.db, world.security, 32768)
        with pytest.raises(AccessDenied):
            await repo.dispatch(task.task_id, world.node, scope, uuid4(), uuid4(), 10)
        async with world.db.connection() as conn:
            assert (await one(conn, "SELECT count(*) FROM task_attempts"))[0] == 0
            assert (await one(conn, "SELECT count(*) FROM execution_grants"))[0] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("table", ["execution_grants", "task_attempts", "task_events", "audit_events"])
def test_dispatch_failure_rolls_back_grant_attempt_and_state(world, table):
    async def scenario():
        task = await ready(world)
        async with world.db.transaction() as conn:
            await conn.execute(f"CREATE TRIGGER injected BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected'); END")
        repo = ExecutionRepository(world.db, world.security, 32768)
        with pytest.raises(sqlite3.IntegrityError):
            await repo.dispatch(task.task_id, world.node, world.scope, uuid4(), uuid4(), 10)
        assert await world.tasks.get(task.task_id, world.owner_id) == task
        async with world.db.connection() as conn:
            for name in ("task_attempts", "execution_grants"):
                assert (await one(conn, f"SELECT count(*) FROM {name}"))[0] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["verifying", "succeeded"])
def test_result_or_verification_write_failure_keeps_recoverable_evidence(world, phase):
    async def scenario():
        repo, command, _ = await dispatched(world)
        result = result_for(command)
        if phase == "succeeded":
            await repo.store_result(world.node, result)
        before = await world.tasks.get(command.task_id, world.owner_id)
        async with world.db.transaction() as conn:
            await conn.execute(f"CREATE TRIGGER injected BEFORE INSERT ON audit_events WHEN NEW.kind='task.{phase}' BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with pytest.raises(sqlite3.IntegrityError):
            if phase == "verifying":
                await repo.store_result(world.node, result)
            else:
                await repo.verify(command.attempt_id)
        assert await world.tasks.get(command.task_id, world.owner_id) == before
        async with world.db.transaction() as conn:
            row = await one(conn, "SELECT result_json FROM task_attempts")
            assert (row[0] is not None) == (phase == "succeeded")
            await conn.execute("DROP TRIGGER injected")
        await repo.store_result(world.node, result)
        await repo.verify(command.attempt_id)
        assert (await world.tasks.get(command.task_id, world.owner_id)).status == "succeeded"
    asyncio.run(scenario())


def test_concurrent_dispatch_has_one_durable_slot_and_no_new_attempt(world):
    async def scenario():
        tasks = [await ready(world) for _ in range(2)]
        repo = ExecutionRepository(world.db, world.security, 32768)
        commands = await asyncio.gather(*(repo.dispatch(t.task_id, world.node, world.scope, uuid4(), uuid4(), 10) for t in tasks))
        assert sum(c is not None for c in commands) == 1
        command = next(c for c in commands if c is not None)
        async with world.db.connection() as conn:
            assert (await one(conn, "SELECT count(*) FROM task_attempts"))[0] == 1
        # No transport ack or disconnect can make this execution safe to repeat.
        with pytest.raises(AccessDenied):
            await repo.dispatch(command.task_id, world.node, world.scope, uuid4(), uuid4(), 10)
    asyncio.run(scenario())
