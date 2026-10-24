"""Schema 021 -> 022 retains old conversations, turns and unknown costs."""

import asyncio
import hashlib
import json
import sqlite3
from importlib.resources import files
from uuid import uuid4

from nexus.core.storage.database import Database,timestamp,utc_now


def test_attachment_upgrade_has_verified_backup_and_no_implicit_disclosure(tmp_path):
    path=tmp_path/"core.sqlite3"; now=timestamp(utc_now())
    owner,conv,turn=map(str,(uuid4(),uuid4(),uuid4()))
    with sqlite3.connect(path) as conn:
        for resource in sorted(files("nexus.core.storage").joinpath("migrations").iterdir(),key=lambda item:item.name):
            if not resource.name.endswith(".sql") or int(resource.name[:3])>21:
                continue
            source=resource.read_text(encoding="utf-8")
            conn.executescript(source)
            if resource.name.startswith("001"):
                conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)")
            conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)",(int(resource.name[:3]),hashlib.sha256(source.encode()).hexdigest(),now))
        conn.execute("INSERT INTO identities VALUES(?, 'owner', ?, NULL, ?)",(owner,"test-hash",now))
        conn.execute("INSERT INTO conversations(conversation_id,owner_id,request_id,title,created_at) VALUES(?,?,?,?,?)",(conv,owner,str(uuid4()),"old conversation",now))
        conn.execute("INSERT INTO chat_turns(turn_id,conversation_id,owner_id,request_id,request_json,text,context_json,context_digest,context_count,context_truncated,status,created_at,expires_at,response_text) VALUES(?,?,?,?,?,?,?,?,0,0,'completed',?,?,'old reply')",
                     (turn,conv,owner,str(uuid4()),json.dumps({"include_context":True}),"old message","[]","a"*64,now,now))
        conn.execute("INSERT INTO model_calls(call_id,provider,model,cost_status,created_at,reserved_microunits,owner_id) VALUES(?,'deepseek','historical','unknown',?,50000,?)",(str(uuid4()),now,owner))
        tables=[row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        before={table:conn.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables}
        columns={table:[row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')] for table in tables}
    asyncio.run(Database(path).initialize())
    backups=list(tmp_path.glob("*.before-v22-*.sqlite3"))
    assert len(backups)==1
    with sqlite3.connect(path) as conn,sqlite3.connect(backups[0]) as backup:
        for table,rows in before.items():
            assert backup.execute(f'SELECT * FROM "{table}"').fetchall()==rows
            names=','.join('"'+name+'"' for name in columns[table])
            after=conn.execute(f'SELECT {names} FROM "{table}"').fetchall()
            if table=="schema_migrations":
                expected=sum(p.name.endswith('.sql') for p in files('nexus.core.storage').joinpath('migrations').iterdir())
                assert after[:21]==rows and len(after)==expected
            elif table=="chat_turns":
                assert after==rows
                assert conn.execute('SELECT attachments_json FROM chat_turns').fetchone()==('[]',)
            else:
                assert after==rows
        assert conn.execute('SELECT permission_override,permission_revision,plan_mode FROM conversations').fetchone()==(None,1,0)
        assert conn.execute("SELECT count(*) FROM chat_attachments").fetchone()[0]==0
        assert conn.execute("SELECT count(*) FROM chat_turn_attachments").fetchone()[0]==0
        assert conn.execute("PRAGMA quick_check").fetchone()[0]=="ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall()==[]
