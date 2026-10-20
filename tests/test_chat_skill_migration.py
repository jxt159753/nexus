"""A populated v12 database upgrades without reinterpreting old evidence."""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from importlib.resources import files
from uuid import uuid4

from nexus.core.storage.database import Database, timestamp, utc_now


def test_v12_chat_history_cost_and_request_snapshot_survive_v13_upgrade(tmp_path):
    path, now = tmp_path / "core.sqlite3", timestamp(utc_now())
    owner, conversation, turn, request_id = (str(uuid4()) for _ in range(4))
    request = json.dumps({"request_id": request_id, "expected_revision": 1, "text": "旧聊天",
                          "allow_model": True, "allow_tools": False, "include_context": True, "node_id": None})
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)")
        for resource in sorted(files("nexus.core.storage").joinpath("migrations").iterdir(), key=lambda p: p.name):
            if resource.name.endswith(".sql") and int(resource.name[:3]) <= 12:
                source = resource.read_text(encoding="utf-8")
                conn.executescript(source)
                conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (int(resource.name[:3]), hashlib.sha256(source.encode()).hexdigest(), now))
                conn.commit()
        conn.execute("INSERT INTO identities VALUES(?,'owner','historical-hash',NULL,?)", (owner, now))
        conn.execute("INSERT INTO conversations(conversation_id,owner_id,request_id,title,created_at) VALUES(?,?,?,?,?)", (conversation, owner, str(uuid4()), "旧会话", now))
        conn.execute("INSERT INTO chat_turns(turn_id,conversation_id,owner_id,request_id,request_json,text,context_json,context_digest,context_count,context_truncated,status,created_at,expires_at,response_text) VALUES(?,?,?,?,?,'旧聊天','[]',?,0,0,'completed',?,?,'旧回答')", (turn, conversation, owner, request_id, request, "a"*64, now, now))
        conn.execute("INSERT INTO model_calls(call_id,provider,model,cost_status,created_at,owner_id,reserved_microunits,outcome,chat_turn_id) VALUES(?,'deepseek','historical','unknown',?,?,50000,'unknown',?)", (str(uuid4()), now, owner, turn))
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'") if row[0] != "schema_migrations"]
        columns = {table: [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')] for table in tables}
        before = {table: conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() for table in tables}
        conn.commit()
    asyncio.run(Database(path).initialize())
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok" and not conn.execute("PRAGMA foreign_key_check").fetchall()
        for table in tables:
            names = ",".join(f'"{name}"' for name in columns[table])
            assert conn.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid').fetchall() == before[table]
        assert conn.execute("SELECT skill_catalog_json,skill_plan_json,skill_run_id FROM chat_turns").fetchone() == ("[]", None, None)
        assert conn.execute("SELECT cost_status,reserved_microunits,skill_run_id FROM model_calls").fetchone() == ("unknown", 50000, None)
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 17
    backups = list(tmp_path.glob("core.sqlite3.before-v13-*.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as conn:
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 12
        assert conn.execute("SELECT request_json FROM chat_turns").fetchone()[0] == request
