"""Non-empty schema 020 upgrades without granting the new image capability."""

import asyncio
import hashlib
import sqlite3
from importlib.resources import files
from uuid import uuid4

from nexus.core.storage.database import Database, timestamp, utc_now


def test_schema_020_image_upgrade_preserves_old_rows_and_backup(tmp_path):
    path = tmp_path/"core.sqlite3"
    now = timestamp(utc_now())
    owner, node = str(uuid4()), str(uuid4())
    with sqlite3.connect(path) as conn:
        for resource in sorted(files("nexus.core.storage").joinpath("migrations").iterdir(),key=lambda item:item.name):
            if not resource.name.endswith(".sql") or int(resource.name[:3]) > 20:
                continue
            source = resource.read_text(encoding="utf-8")
            conn.executescript(source)
            if resource.name.startswith("001"):
                conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)")
            conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)",(int(resource.name[:3]),hashlib.sha256(source.encode()).hexdigest(),now))
        for identity, role in ((owner,"owner"),(node,"node")):
            conn.execute("INSERT INTO identities VALUES(?,?,?,NULL,?)",(identity,role,hashlib.sha256(identity.encode()).hexdigest(),now))
            conn.execute("INSERT INTO grants VALUES(?,?,?,1,'workspace')",(str(uuid4()),identity,"filesystem.read"))
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        before = {table:conn.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables}
    asyncio.run(Database(path).initialize())
    backups = list(tmp_path.glob("*.before-v21-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(path) as conn, sqlite3.connect(backups[0]) as backup:
        for table, rows in before.items():
            assert backup.execute(f'SELECT * FROM "{table}"').fetchall() == rows
            actual = conn.execute(f'SELECT * FROM "{table}"').fetchall()
            assert actual[:20] == rows if table == "schema_migrations" else actual == rows
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 26
        assert conn.execute("SELECT count(*) FROM grants WHERE tool='filesystem.read_image'").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
