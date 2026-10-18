"""S5 authority: stopped evidence, bounded retries, controls and terminal races."""

import asyncio
import hashlib
import sqlite3
import threading
from contextlib import closing
from datetime import timedelta
from importlib.resources import files
from uuid import uuid4

import pytest

from nexus.contracts.errors import ErrorCode
from nexus.contracts.commands import command_identity
from nexus.contracts.messages import CommandStatusResult
from nexus.core.storage.control import ControlRepository
from nexus.core.storage.database import Database, one, timestamp, utc_now
from nexus.core.storage.execution import ExecutionRepository
from nexus.core.storage.tasks import Conflict
from nexus.core.security.types import AccessDenied
from nexus.node.receipts import ReceiptStore
from nexus.node.runtime import NodeRuntime
from node_support import command, configuration
from test_harness import dispatched, ready, result_for


def status_for(command, phase, *, stopped=False):
    return CommandStatusResult(session_id=command.session_id, attempt_id=command.attempt_id,
        phase=phase, command_digest=None if phase == "not_found" else command.command_digest,
        execution_stopped=stopped)


async def retry(world, repo, command):
    await world.security.prepare_pending(command.task_id, world.node, world.scope)
    return await repo.dispatch(command.task_id, world.node, world.scope, uuid4(), uuid4(), 10)


def test_unknown_requires_positive_stopped_evidence_and_late_old_result_is_audit_only(world):
    async def scenario():
        repo, first, _ = await dispatched(world)
        await repo.status(world.node, status_for(first, "unknown"))
        assert (await world.tasks.get(first.task_id, world.owner_id)).status == "recovering"
        assert not await repo.pending(world.node_id)
        assert len(await repo.active(world.node_id)) == 1
        await repo.status(world.node, status_for(first, "unknown", stopped=True))
        second = await retry(world, repo, first)
        assert second.attempt_id != first.attempt_id
        assert await repo.store_result(world.node, result_for(first)) == "already_settled"
        current = await world.tasks.get(first.task_id, world.owner_id)
        assert current.current_attempt_id == second.attempt_id and current.status == "dispatching"
        await repo.status(world.node, status_for(second, "unknown", stopped=True))
        task = await world.tasks.get(first.task_id, world.owner_id)
        assert task.status == "failed" and task.error.code == ErrorCode.BUDGET_EXCEEDED
        assert task.execution_outcome == "unknown"
        assert len((await repo.detail(task.task_id, world.owner_id))["attempts"]) == 2
        assert not await repo.pending(world.node_id)
    asyncio.run(scenario())


def test_tool_timeouts_retry_only_twice_then_stop(world):
    async def scenario():
        repo, first, _ = await dispatched(world)
        current = first
        for attempt in (1, 2):
            failure = result_for(current, outcome="failed", result=None,
                error={"code": "TOOL_TIMEOUT", "message": "controlled timeout", "retryable": True})
            await repo.store_result(world.node, failure)
            await repo.verify(current.attempt_id)
            task = await world.tasks.get(first.task_id, world.owner_id)
            assert task.execution_outcome == "known"
            if attempt == 1:
                assert task.status == "recovering"
                current = await retry(world, repo, first)
            else:
                assert task.status == "failed" and task.error.code == ErrorCode.BUDGET_EXCEEDED
        assert not await repo.active(world.node_id)
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["not_found", "received"])
def test_retransmit_keeps_identity_three_dispatches_and_stops_before_freeing_slot(world, phase):
    async def scenario():
        repo, item, _ = await dispatched(world)
        for _ in range(2):
            assert await repo.status(world.node, status_for(item, phase)) == "resend"
            again = await repo.resend(world.node, world.scope, item.attempt_id, uuid4())
            assert again.attempt_id == item.attempt_id and again.command_digest == item.command_digest
        assert await repo.resend(world.node, world.scope, item.attempt_id, uuid4()) is None
        task = await world.tasks.get(item.task_id, world.owner_id)
        assert task.status == "failed" and task.execution_outcome == "unknown"
        other = await ready(world)
        assert await repo.dispatch(other.task_id, world.node, world.scope, uuid4(), uuid4(), 10) is None
        await repo.cancelled(world.node, item.attempt_id)
        assert (await world.tasks.get(item.task_id, world.owner_id)).status == "failed"
        assert await repo.dispatch(other.task_id, world.node, world.scope, uuid4(), uuid4(), 10) is not None
        detail = await repo.detail(item.task_id, world.owner_id)
        assert detail["attempts"][0]["dispatch_count"] == 3
    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["ready", "awaiting_approval", "running", "verifying"])
def test_cancel_intent_and_completion_precedence(world, state):
    async def scenario():
        controls = ControlRepository(world.db)
        repo = ExecutionRepository(world.db, world.security, 32768)
        if state in {"ready", "awaiting_approval"}:
            task = await ready(world, directory=state == "awaiting_approval", approve=False)
            result = await controls.cancel(world.owner, task.task_id)
            assert result["task"].status == "cancelled"
            assert result["task"].execution_outcome == "not_started"
            return
        repo, item, _ = await dispatched(world)
        if state == "verifying":
            await repo.store_result(world.node, result_for(item))
        result = await controls.cancel(world.owner, item.task_id)
        if state == "verifying":
            assert result["disposition"] == "already_finished"
            await repo.verify(item.attempt_id)
            assert (await world.tasks.get(item.task_id, world.owner_id)).status == "succeeded"
        else:
            assert result["disposition"] == "requested" and result["task"].cancel_requested
            assert result["task"].status == "dispatching"
            await repo.status(world.node, status_for(item, "unknown"))
            assert (await world.tasks.get(item.task_id, world.owner_id)).status != "cancelled"
            await repo.cancelled(world.node, item.attempt_id)
            task = await world.tasks.get(item.task_id, world.owner_id)
            assert task.status == "cancelled" and task.execution_outcome == "known"
            late = result_for(item)
            assert await repo.store_result(world.node, late) == "already_settled"
            assert (await world.tasks.get(item.task_id, world.owner_id)).status == "cancelled"
    asyncio.run(scenario())


@pytest.mark.parametrize("active", [False, True])
def test_deadline_including_offline_wait_is_terminal_but_execution_is_unknown_until_reconciled(world, active):
    async def scenario():
        if active:
            repo, item, _ = await dispatched(world)
            task_id = item.task_id
        else:
            task_id = (await ready(world, directory=True, approve=False)).task_id
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE tasks SET expires_at=? WHERE task_id=?", (timestamp(utc_now() - timedelta(seconds=1)), str(task_id)))
        await ControlRepository(world.db).expire()
        task = await world.tasks.get(task_id, world.owner_id)
        assert task.status == "timed_out" and task.error.code == ErrorCode.TOOL_TIMEOUT
        assert task.execution_outcome == ("unknown" if active else "not_started")
        if active:
            assert await repo.store_result(world.node, result_for(item)) == "already_settled"
            task = await world.tasks.get(task_id, world.owner_id)
            assert task.status == "timed_out" and task.result is None and task.execution_outcome == "known"
    asyncio.run(scenario())


def test_controls_persist_idempotency_and_cancel_all_does_not_cancel_later_tasks(world):
    async def scenario():
        controls = ControlRepository(world.db)
        first = await ready(world)
        pause_id = uuid4()
        paused = await controls.apply(world.owner, "pause", pause_id)
        assert paused["paused"]
        repo = ExecutionRepository(world.db, world.security, 32768)
        assert await repo.dispatch(first.task_id, world.node, world.scope, uuid4(), uuid4(), 10) is None
        reopened = ControlRepository(Database(world.db.path))
        assert (await reopened.state())["paused"]
        cancel_id = uuid4()
        cancelled = await controls.apply(world.owner, "cancel_all", cancel_id)
        assert cancelled["affected"] == 1 and cancelled["paused"]
        second = await ready(world)
        assert await controls.apply(world.owner, "cancel_all", cancel_id) == cancelled
        assert (await world.tasks.get(second.task_id, world.owner_id)).status == "ready"
        with pytest.raises(Conflict):
            await controls.apply(world.owner, "resume", pause_id)
        await controls.apply(world.owner, "resume", uuid4())
        assert await controls.apply(world.owner, "pause", pause_id) == paused
        assert not (await controls.state())["paused"]
        assert await repo.dispatch(second.task_id, world.node, world.scope, uuid4(), uuid4(), 10)
    asyncio.run(scenario())


def test_retry_rechecks_changed_directory_approval_and_dispatch_authority(world):
    async def scenario():
        repo, item, _ = await dispatched(world, directory=True)
        await repo.status(world.node, status_for(item, "unknown", stopped=True))
        await world.security.set_grant(world.owner_id, "system.info")
        auth = await world.security.prepare_pending(item.task_id, world.node, world.scope)
        assert auth.mode == "approval_required"
        await world.security.decide(auth.approval_id, world.owner, "approve", world.scope)
        second = await repo.dispatch(item.task_id, world.node, world.scope, uuid4(), uuid4(), 10)
        assert second.attempt_id != item.attempt_id
        assert len((await repo.detail(item.task_id, world.owner_id))["approvals"]) == 2
    asyncio.run(scenario())


def test_control_evidence_write_failure_rolls_back_every_task(world):
    async def scenario():
        task = await ready(world)
        async with world.db.transaction() as conn:
            await conn.execute("CREATE TRIGGER injected BEFORE INSERT ON audit_events WHEN NEW.kind='control.cancel_all' BEGIN SELECT RAISE(ABORT,'injected'); END")
        controls = ControlRepository(world.db)
        with pytest.raises(sqlite3.IntegrityError):
            await controls.apply(world.owner, "cancel_all", uuid4())
        assert await world.tasks.get(task.task_id, world.owner_id) == task
        assert (await controls.state())["revision"] == 0
    asyncio.run(scenario())


def test_cancel_result_race_has_one_terminal_state(world):
    async def scenario():
        repo, item, _ = await dispatched(world)
        async def complete():
            await repo.store_result(world.node, result_for(item))
            await repo.verify(item.attempt_id)
        await asyncio.gather(ControlRepository(world.db).cancel(world.owner, item.task_id), complete())
        task = await world.tasks.get(item.task_id, world.owner_id)
        assert task.status == "succeeded"  # Already completed evidence wins pending cancellation.
        detail = await repo.detail(item.task_id, world.owner_id)
        assert sum(e["kind"] in {"succeeded", "failed", "timed_out", "cancelled"} for e in detail["events"]) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("received", [False, True])
def test_persisted_node_cancel_fences_delayed_command_across_restart(tmp_path, received):
    cfg = configuration(tmp_path)
    item = command(cfg)
    with NodeRuntime(cfg) as runtime:
        if received:
            runtime.receipts.accept(item, runtime.boot_id)
        runtime.cancel(item.attempt_id)
    with NodeRuntime(cfg) as runtime:
        runtime._worker.run = lambda *_: pytest.fail("cancelled command must not start")
        status = runtime.execute(item)
        assert status.phase == "cancelled" and status.execution_stopped


def test_node_v1_upgrade_preserves_receipt_and_verified_backup(tmp_path):
    cfg = configuration(tmp_path)
    path = tmp_path / "receipts.sqlite3"
    script = files("nexus.node").joinpath("migrations/001_receipts.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(script)
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_migrations VALUES(1,?)", (hashlib.sha256(script.encode()).hexdigest(),))
        conn.execute("INSERT INTO node_identity VALUES(?)", (str(cfg.node_id),))
    store = ReceiptStore(path, cfg.node_id)
    item = command(cfg)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO execution_receipts(attempt_id,task_id,command_digest,identity_json,boot_id,phase) VALUES(?,?,?,?,?,'received')", (
            str(item.attempt_id), str(item.task_id), item.command_digest, command_identity(item, cfg.node_id), str(uuid4())))
    store.initialize()
    assert store.get(item.attempt_id).phase == "received"
    backups = list(tmp_path.glob("*.before-v2-*.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM execution_receipts").fetchone()[0] == 1


def test_cancel_during_async_sqlite_open_closes_the_actual_connection(tmp_path, monkeypatch):
    import aiosqlite
    opened, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = aiosqlite.connect
    class TrackedConnection(sqlite3.Connection):
        def close(self):
            try:
                super().close()
            finally:
                closed.set()
    def delayed(*args, **kwargs):
        conn = original(*args, factory=TrackedConnection, **kwargs)
        connector = conn._connector
        def connect():
            opened.set()
            assert release.wait(3)
            return connector()
        conn._connector = connect
        return conn
    monkeypatch.setattr(aiosqlite, "connect", delayed)
    async def scenario():
        async def work():
            async with Database(tmp_path / "cancel.sqlite3").connection():
                pytest.fail("cancelled open must not enter caller transaction")
        task = asyncio.create_task(work())
        assert await asyncio.to_thread(opened.wait, 3)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
    asyncio.run(scenario())


def test_deadline_after_durable_result_releases_slot_without_claiming_success(world):
    async def scenario():
        repo, item, _ = await dispatched(world)
        await repo.store_result(world.node, result_for(item))
        async with world.db.transaction() as conn:
            await conn.execute("UPDATE tasks SET expires_at=? WHERE task_id=?", (timestamp(utc_now() - timedelta(seconds=1)), str(item.task_id)))
        await ControlRepository(world.db).expire()
        await repo.resume_verification()
        task = await world.tasks.get(item.task_id, world.owner_id)
        assert task.status == "timed_out" and task.execution_outcome == "known" and task.result is None
        assert not await repo.active(world.node_id)
    asyncio.run(scenario())


def test_reconciliation_budget_is_persistent_and_does_not_release_unknown_execution(world):
    async def scenario():
        repo, item, _ = await dispatched(world)
        for _ in range(64):
            assert await repo.reserve_probe(item.attempt_id)
        reopened = ExecutionRepository(Database(world.db.path), world.security, 32768)
        assert not await reopened.reserve_probe(item.attempt_id)
        assert len(await reopened.active(world.node_id)) == 1
        assert (await world.tasks.get(item.task_id, world.owner_id)).execution_outcome == "unknown"
    asyncio.run(scenario())


def test_cancel_all_requires_owner_and_waits_for_active_stop(world):
    async def scenario():
        repo, item, _ = await dispatched(world)
        queued = await ready(world)
        controls = ControlRepository(world.db)
        with pytest.raises(AccessDenied):
            await controls.apply(world.node, "cancel_all", uuid4())
        result = await controls.apply(world.owner, "cancel_all", uuid4())
        assert result["affected"] == 2 and result["paused"] is False
        assert (await world.tasks.get(item.task_id, world.owner_id)).cancel_requested
        assert (await world.tasks.get(queued.task_id, world.owner_id)).status == "cancelled"
        await repo.status(world.node, status_for(item, "unknown"))
        assert (await world.tasks.get(item.task_id, world.owner_id)).status != "cancelled"
        await repo.cancelled(world.node, item.attempt_id)
        assert (await world.tasks.get(item.task_id, world.owner_id)).status == "cancelled"
    asyncio.run(scenario())


def test_core_v3_upgrade_preserves_active_and_completed_attempts(world, tmp_path):
    async def seed():
        repo, completed, _ = await dispatched(world)
        await repo.store_result(world.node, result_for(completed))
        await repo.verify(completed.attempt_id)
        task = await ready(world)
        active = await repo.dispatch(task.task_id, world.node, world.scope, uuid4(), uuid4(), 10)
        return completed, active
    completed, active = asyncio.run(seed())
    path = tmp_path / "s4.sqlite3"
    with closing(sqlite3.connect(path)) as target, closing(sqlite3.connect(world.db.path)) as source, target:
        target.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)")
        for name in ("001_tasks.sql", "002_security.sql", "003_execution.sql"):
            script = files("nexus.core.storage").joinpath("migrations/" + name).read_text(encoding="utf-8")
            target.executescript(script)
            target.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (int(name[:3]), hashlib.sha256(script.encode()).hexdigest(), timestamp(utc_now())))
        tables = [r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0] not in {"schema_migrations", "sqlite_sequence"}]
        snapshots = {}
        for table in tables:
            columns = ",".join(r[1] for r in target.execute(f"PRAGMA table_info({table})"))
            rows = source.execute(f"SELECT {columns} FROM {table}").fetchall()
            target.execute(f"DELETE FROM {table}")
            placeholders = ",".join("?" for _ in columns.split(","))
            target.executemany(f"INSERT INTO {table}({columns}) VALUES({placeholders})", rows)
            snapshots[table] = (columns, rows)
    asyncio.run(Database(path).initialize())
    backups = list(tmp_path.glob("s4.sqlite3.before-v4-*.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(path)) as target, closing(sqlite3.connect(backups[0])) as backup:
        for table, (columns, rows) in snapshots.items():
            assert target.execute(f"SELECT {columns} FROM {table}").fetchall() == rows
            assert backup.execute(f"SELECT {columns} FROM {table}").fetchall() == rows
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 3
        assert target.execute("SELECT execution_stopped FROM task_attempts WHERE attempt_id=?", (str(completed.attempt_id),)).fetchone()[0] == 1
        assert target.execute("SELECT execution_stopped FROM task_attempts WHERE attempt_id=?", (str(active.attempt_id),)).fetchone()[0] == 0
        assert target.execute("PRAGMA foreign_key_check").fetchall() == []
