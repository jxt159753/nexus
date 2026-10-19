"""Non-empty schema 010 -> 011 migration preserves old rows and makes a backup."""

import asyncio
import hashlib
import sqlite3
from contextlib import closing
from importlib.resources import files
from uuid import uuid4

from nexus.core.storage.database import Database, timestamp, utc_now


def test_nonempty_v10_upgrade_preserves_ledger_and_creates_extension_tables(tmp_path):
    path = tmp_path / "core.sqlite3"
    now = timestamp(utc_now())
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
        for resource in sorted(files("nexus.core.storage").joinpath("migrations").iterdir(), key=lambda item: item.name):
            if resource.name.endswith(".sql") and int(resource.name[:3]) <= 10:
                source = resource.read_text(encoding="utf-8")
                conn.executescript(source)
                conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)",
                             (int(resource.name[:3]), hashlib.sha256(source.encode()).hexdigest(), now))
        owner = str(uuid4())
        conn.execute("INSERT INTO identities VALUES(?, 'owner', ?, NULL, ?)", (owner, "safe-digest", now))
        call = str(uuid4())
        conn.execute("INSERT INTO model_calls(call_id,provider,model,cost_microunits,cost_status,created_at,owner_id,reserved_microunits,outcome) VALUES(?, 'deepseek','historical',17,'calculated',?,?,50,'completed')", (call, now, owner))
        before = {table: conn.execute(f"SELECT * FROM {table}").fetchall()
                  for table in ("identities", "model_calls", "schema_migrations")}
        conn.commit()
    asyncio.run(Database(path).initialize())
    backups = list(tmp_path.glob("core.sqlite3.before-v11-*.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 12
        for table, rows in before.items():
            if table == "schema_migrations":
                continue
            expected = [(*row, None) for row in rows] if table == "model_calls" else rows
            assert conn.execute(f"SELECT * FROM {table}").fetchall() == expected
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"extension_requests", "extension_versions", "extension_tests",
                "extension_approvals", "extension_enablements", "skill_runs",
                "skill_run_steps", "extension_tool_runs"} <= tables
        assert conn.execute("PRAGMA table_info(model_calls)").fetchall()[-1][1] == "extension_request_id"
    with closing(sqlite3.connect(backups[0])) as backup:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 10
        assert backup.execute("SELECT * FROM model_calls").fetchall() == before["model_calls"]
