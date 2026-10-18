"""Versioned SQLite, short isolated transactions and verified migration backups."""

import asyncio
import hashlib
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import aiosqlite


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("UTC datetime required")
    return value.isoformat(timespec="microseconds")


async def one(conn: aiosqlite.Connection, sql: str, args: tuple = ()) -> aiosqlite.Row | None:
    async with conn.execute(sql, args) as cursor:
        return await cursor.fetchone()


async def audit(
    conn: aiosqlite.Connection, actor_id: str | None, task_id: str | None,
    kind: str, now: datetime, *, subject_id: str | None = None,
) -> None:
    # Deliberately no free-form input, credential, or tool-output column.
    await conn.execute(
        "INSERT INTO audit_events(actor_id, subject_id, task_id, kind, created_at) VALUES (?, ?, ?, ?, ?)",
        (actor_id, subject_id, task_id, kind, timestamp(now)),
    )


class Database:
    def __init__(self, path: Path):
        if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
            raise ValueError("database must be an absolute local path")
        self.path = path

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        # A scheduler/transport may be cancelled while the worker thread opens
        # SQLite. Keep that result owned until it can be explicitly closed.
        conn = aiosqlite.connect(self.path, isolation_level=None, timeout=3)
        opening = asyncio.ensure_future(conn)
        try:
            await asyncio.shield(opening)
        except asyncio.CancelledError:
            try:
                await opening
            finally:
                await conn.close()
            raise
        try:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("PRAGMA busy_timeout = 3000")
            await conn.execute("PRAGMA synchronous = FULL")
            yield conn
        finally:
            closing_task = asyncio.create_task(conn.close())
            try:
                await asyncio.shield(closing_task)
            except asyncio.CancelledError:
                await closing_task
                raise

    @asynccontextmanager
    async def transaction(self, *, write: bool = True) -> AsyncIterator[aiosqlite.Connection]:
        async with self.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def initialize(self) -> None:
        """Offline/startup only. Never adopt or reset an unversioned database."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        migrations = sorted(files(__package__).joinpath("migrations").iterdir(), key=lambda p: p.name)
        scripts = [(int(p.name[:3]), p.read_text(encoding="utf-8")) for p in migrations if p.name.endswith(".sql")]
        if [v for v, _ in scripts] != list(range(1, len(scripts) + 1)):
            raise RuntimeError("non-contiguous migration resources")
        async with self.connection() as conn:
            tables = await conn.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table'")
            names = {row[0] for row in tables}
            if names and "schema_migrations" not in names:
                raise RuntimeError("unversioned database: manual migration required")
            applied = [] if not names else await conn.execute_fetchall(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            )
            if [row[0] for row in applied] != list(range(1, len(applied) + 1)) or len(applied) > len(scripts):
                raise RuntimeError("unknown or incomplete schema version")
            for row, (_, script) in zip(applied, scripts):
                if row[1] != hashlib.sha256(script.encode()).hexdigest():
                    raise RuntimeError("migration checksum mismatch")
            pending = scripts[len(applied):]
            if not pending:
                return
            if names:
                backup_path = self.path.with_name(f"{self.path.name}.before-v{pending[0][0]}-{uuid4().hex}.sqlite3")
                with closing(sqlite3.connect(backup_path)) as backup:
                    await conn.backup(backup)
                    if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("migration backup verification failed")
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
                for version, script in pending:
                    statement = ""
                    for line in script.splitlines(keepends=True):
                        statement += line
                        if sqlite3.complete_statement(statement):
                            await conn.execute(statement)
                            statement = ""
                    if statement.strip():
                        raise RuntimeError("incomplete SQL migration")
                    await conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (
                        version, hashlib.sha256(script.encode()).hexdigest(), timestamp(utc_now()),
                    ))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
