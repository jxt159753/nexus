import asyncio
import hashlib
import sqlite3
from contextlib import closing
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest

from nexus.contracts.task import SystemInfoArguments, SystemInfoRequest
from nexus.core.storage.database import Database, one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict, TaskRepository


def request():
    return SystemInfoRequest(request_id=uuid4(), intent="get_system_info", arguments=SystemInfoArguments())


def test_reopen_idempotency_and_history(world):
    async def scenario():
        req = request()
        first = await world.tasks.create(world.owner_id, req, world.node_id)
        again = await world.tasks.create(world.owner_id, req, world.node_id)
        assert first == again
        db = Database(world.db.path)
        await db.initialize()
        repo = TaskRepository(db)
        assert await repo.get(first.task_id, world.owner_id) == first
        assert len(await repo.history(world.owner_id)) == 1
        assert await repo.history(uuid4()) == []
        with pytest.raises(PermissionError):
            await repo.get(first.task_id, uuid4())
        assert not list(world.db.path.parent.glob("*.before-*"))
        async with db.connection() as conn:
            assert (await one(conn, "PRAGMA journal_mode"))[0] == "wal"
            assert (await one(conn, "PRAGMA foreign_keys"))[0] == 1
            assert (await one(conn, "PRAGMA busy_timeout"))[0] == 3000
            assert (await one(conn, "PRAGMA synchronous"))[0] == 2
    asyncio.run(scenario())


def test_idempotency_includes_node_and_arguments(world):
    from nexus.contracts.task import ListDirectoryArguments, ListDirectoryRequest
    from pydantic import SecretStr
    import secrets

    async def scenario():
        req = ListDirectoryRequest(request_id=uuid4(), intent="list_directory", arguments=ListDirectoryArguments(root_id="workspace", limit=10))
        await world.tasks.create(world.owner_id, req, world.node_id)
        different = req.model_copy(update={"arguments": ListDirectoryArguments(root_id="workspace", limit=11)})
        with pytest.raises(Conflict, match="IDEMPOTENCY_CONFLICT"):
            await world.tasks.create(world.owner_id, different, world.node_id)
        other_node = uuid4()
        await world.security.register_identity(other_node, "node", SecretStr(secrets.token_urlsafe(32)))
        with pytest.raises(Conflict, match="IDEMPOTENCY_CONFLICT"):
            await world.tasks.create(world.owner_id, req, other_node)
        assert len(await world.tasks.history(world.owner_id)) == 1
    asyncio.run(scenario())


def test_concurrent_creation_and_revision_compare(world):
    async def scenario():
        req = request()
        tasks = await asyncio.gather(*(world.tasks.create(world.owner_id, req, world.node_id) for _ in range(8)))
        assert len({t.task_id for t in tasks}) == 1
        task = tasks[0]
        results = await asyncio.gather(*(
            world.tasks.transition(task.task_id, world.owner_id, 0, status)
            for status in ("cancelled", "failed")
        ), return_exceptions=True)
        assert sum(isinstance(r, Conflict) for r in results) == 1
        assert len(await world.tasks.history(world.owner_id)) == 2
        assert (await world.tasks.get(task.task_id, world.owner_id)).revision == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["succeeded", "running", "dispatching", "verifying", "recovering"])
def test_illegal_or_unimplemented_execution_transitions_rollback(world, status):
    async def scenario():
        task = await world.tasks.create(world.owner_id, request(), world.node_id)
        with pytest.raises(Conflict, match="illegal"):
            await world.tasks.transition(task.task_id, world.owner_id, 0, status)
        assert (await world.tasks.get(task.task_id, world.owner_id)) == task
        assert len(await world.tasks.history(world.owner_id)) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("table", ["task_events", "audit_events"])
def test_create_and_transition_rollback_if_evidence_write_fails(world, table):
    async def scenario():
        task = await world.tasks.create(world.owner_id, request(), world.node_id)
        async with world.db.transaction() as conn:
            await conn.execute(f"CREATE TRIGGER deny_write BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            await world.tasks.create(world.owner_id, request(), world.node_id)
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            await world.tasks.transition(task.task_id, world.owner_id, 0, "cancelled")
        assert (await world.tasks.get(task.task_id, world.owner_id)) == task
        assert len(await world.tasks.history(world.owner_id)) == 1
        async with world.db.connection() as conn:
            assert (await one(conn, "SELECT count(*) FROM tasks"))[0] == 1
            assert (await one(conn, "SELECT count(*) FROM audit_events WHERE task_id IS NOT NULL"))[0] == 1
    asyncio.run(scenario())


def test_failed_commit_does_not_accept_task(world, monkeypatch):
    async def fail_commit(self):
        raise sqlite3.OperationalError("injected disk failure")

    async def scenario():
        with monkeypatch.context() as patch:
            patch.setattr(aiosqlite.Connection, "commit", fail_commit)
            with pytest.raises(sqlite3.OperationalError, match="disk failure"):
                await world.tasks.create(world.owner_id, request(), world.node_id)
        assert await world.tasks.history(world.owner_id) == []
    asyncio.run(scenario())


def test_foreign_keys_and_unknown_owner(world):
    async def scenario():
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            async with world.db.transaction() as conn:
                await conn.execute("INSERT INTO task_events(task_id, revision, kind, created_at) VALUES (?, 0, 'queued', ?)", (str(uuid4()), timestamp(utc_now())))
        for owner, node in ((uuid4(), world.node_id), (world.owner_id, uuid4()), (world.node_id, world.node_id)):
            with pytest.raises(PermissionError):
                await world.tasks.create(owner, request(), node)
    asyncio.run(scenario())


def test_queue_limit_and_bounded_queries(world):
    async def scenario():
        for _ in range(100):
            await world.tasks.create(world.owner_id, request(), world.node_id)
        with pytest.raises(Conflict, match="BUSY"):
            await world.tasks.create(world.owner_id, request(), world.node_id)
        history = await world.tasks.history(world.owner_id, limit=2)
        assert len(history) == 2
        assert len(await world.tasks.history(world.owner_id, after_event_id=history[-1]["event_id"])) == 98
        for limit in (0, 201, True):
            with pytest.raises(ValueError):
                await world.tasks.history(world.owner_id, limit=limit)
    asyncio.run(scenario())


def test_forward_migration_makes_verified_backup(tmp_path):
    path = tmp_path / "core.sqlite3"
    script = files("nexus.core.storage").joinpath("migrations/001_tasks.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(script)
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_migrations VALUES (1, ?, ?)", (hashlib.sha256(script.encode()).hexdigest(), timestamp(utc_now())))
        conn.execute("INSERT INTO identities VALUES (?, 'owner', ?, NULL, ?)", (str(uuid4()), "safe-digest", timestamp(utc_now())))
    asyncio.run(Database(path).initialize())
    backups = list(tmp_path.glob("*.before-v2-*.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM identities").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM identities").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2


@pytest.mark.parametrize("kind", ["unversioned", "newer", "checksum"])
def test_incompatible_database_is_rejected_without_rebuild(tmp_path, kind):
    path = tmp_path / "core.sqlite3"
    if kind != "unversioned":
        asyncio.run(Database(path).initialize())
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE precious (value TEXT)")
        conn.execute("INSERT INTO precious VALUES ('preserve')")
        if kind == "newer":
            conn.execute("INSERT INTO schema_migrations VALUES (3, 'unknown', 'unknown')")
        elif kind == "checksum":
            conn.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=1")
    with pytest.raises(RuntimeError):
        asyncio.run(Database(path).initialize())
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT * FROM precious").fetchall() == [("preserve",)]


def test_failed_migration_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    from nexus.core.storage import database
    scripts = tmp_path / "resources" / "migrations"
    scripts.mkdir(parents=True)
    (scripts / "001_bad.sql").write_text("CREATE TABLE provisional (id INTEGER);\nINVALID SQL;\n", encoding="utf-8")
    monkeypatch.setattr(database, "files", lambda _: tmp_path / "resources")
    path = tmp_path / "core.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(Database(path).initialize())
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


@pytest.mark.parametrize("path", ["relative.sqlite3", "\\\\server\\share\\core.sqlite3"])
def test_local_absolute_database_required(path):
    with pytest.raises(ValueError):
        Database(Path(path))
