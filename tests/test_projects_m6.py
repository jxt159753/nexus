"""M6 isolation, fresh consent, evidence, restart, and no real model traffic."""

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from nexus.contracts.chat import ConversationCreate
from nexus.contracts.records import MemoryCreate, MemorySelection, ProjectCreate, TodoCreate, TodoUpdate
from nexus.core.memory.projects import Projects
from nexus.core.memory.service import Records
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import one, timestamp, utc_now
from nexus.core.storage.tasks import Conflict
from test_chat import app_for, send
from test_harness_http import login, submission
from test_node_link import ORIGIN, core_config, grant
from nexus.core.app import create_app


def test_project_chat_selection_consent_expiry_retraction_and_reopen(world, monkeypatch):
    chat = app_for(world, monkeypatch).state.chat

    async def scenario():
        records = Records(world.db)
        first = await records.create_project(world.owner, ProjectCreate(name="project A"))
        second = await records.create_project(world.owner, ProjectCreate(name="project B"))
        a, b = UUID(first["project_id"]), UUID(second["project_id"])
        request = ConversationCreate(request_id=uuid4(), project_id=a)
        conv = await chat.create(world.owner, request)
        assert (await chat.create(world.owner, request))["conversation_id"] == conv["conversation_id"]
        with pytest.raises(Conflict):
            await chat.create(world.owner, request.model_copy(update={"project_id": b}))
        mem = await records.save(world.owner, MemoryCreate(request_id=uuid4(), consent=True, content="Godot desktop",kind="fact",source="explicit user",project_id=a))
        mid = UUID(mem["memory_id"])
        preview = await chat.memory_preview(world.owner, conv["conversation_id"], MemorySelection(memory_ids=[mid]))
        other = await chat.create(world.owner, ConversationCreate(request_id=uuid4(),project_id=b))
        with pytest.raises(AccessDenied):
            await chat.memory_preview(world.owner, other["conversation_id"], MemorySelection(memory_ids=[mid]))
        turn, value = await send(chat, world, conv["conversation_id"], memory_ids=[mid],memory_digest=preview["memory_digest"],allow_memory_model=True)
        assert turn["memory_references"][0]["source"] == "explicit user"
        await chat.process_one()
        assert "Godot desktop" in chat.provider.requests[0][0]["content"]
        follow, _ = await send(chat, world, conv["conversation_id"])
        assert follow["context_count"] == 0  # Old outbound consent is not reused.
        await chat.process_one()
        assert "Godot desktop" not in json.dumps(chat.provider.requests[-1])
        pending, _ = await send(chat, world, conv["conversation_id"],memory_ids=[mid],memory_digest=preview["memory_digest"],allow_memory_model=True)
        await records.retract(world.owner, mid)
        assert await chat.claim() is None
        assert (await chat.get(world.owner, pending["turn_id"]))["error_code"] == "MEMORY_UNAVAILABLE"
        assert len(chat.provider.requests) == 2
        with pytest.raises(AccessDenied):
            await chat.memory_preview(world.owner, conv["conversation_id"], MemorySelection(memory_ids=[mid]))
        exp = await records.save(world.owner,MemoryCreate(request_id=uuid4(),consent=True,content="expired",kind="plan",source="user",project_id=a,valid_until=utc_now()-timedelta(seconds=1)))
        with pytest.raises(AccessDenied):
            await chat.memory_preview(world.owner,conv["conversation_id"],MemorySelection(memory_ids=[UUID(exp["memory_id"])]))
        assert [row["project_id"] for row in await chat.page(world.owner,project_id=a)] == [str(a)]
        with pytest.raises(ValueError):
            await records.save(world.owner,MemoryCreate(request_id=uuid4(),consent=True,content="wrong project",kind="fact",source=f"chat:{turn['turn_id']}",project_id=b))
        return conv

    conv = asyncio.run(scenario())
    with TestClient(create_app(core_config(world)),base_url=ORIGIN) as client:
        login(client,world)
        history=client.get(f"/api/v1/chat/conversations/{conv['conversation_id']}/turns").json()
        assert history["conversation"]["project_id"]==conv["project_id"]
        assert history["turns"][0]["memory_references"][0]["content"]=="Godot desktop"


def test_todo_evidence_cross_project_idempotency_history_and_restart(world):
    grant(world)
    with TestClient(create_app(core_config(world)),base_url=ORIGIN) as client:
        auth=login(client,world)
        a=client.post("/api/v1/projects",json={"name":"A"},headers=auth).json()["project"]["project_id"]
        b=client.post("/api/v1/projects",json={"name":"B"},headers=auth).json()["project"]["project_id"]
        payload={"request_id":str(uuid4()),"text":"Check machine"}
        todo=client.post(f"/api/v1/projects/{a}/todos",json=payload,headers=auth).json()["todo"]
        assert client.post(f"/api/v1/projects/{a}/todos",json=payload,headers=auth).json()["todo"]==todo
        assert client.post(f"/api/v1/projects/{b}/todos",json=payload,headers=auth).status_code==409
        update={**payload,"request_id":str(uuid4()),"expected_revision":1,"status":"done","evidence_task_id":str(uuid4())}
        url=f"/api/v1/projects/{a}/todos/{todo['todo_id']}"
        assert client.patch(url,json=update,headers=auth).status_code==403
        req=submission(world);req["request"]["project_id"]=a
        task=client.post("/api/v1/tasks",json=req,headers=auth).json()["task"]
        update["evidence_task_id"]=task["task_id"]
        assert client.patch(url,json=update,headers=auth).status_code==403
        # Controlled DB fixture only: real successful evidence is exercised by the
        # separate Windows/Core/Node integration test.
        async def finish():
            async with world.db.transaction() as conn:
                await conn.execute("UPDATE tasks SET status='succeeded',execution_outcome='known',verification='node_report_schema_checked',result_json=? WHERE task_id=?",(json.dumps({"os_name":"test","os_version":"test","architecture":"test","python_version":"test"}),task["task_id"]))
        asyncio.run(finish())
        result=client.patch(url,json=update,headers=auth).json()["todo"]
        assert result["status"]=="done" and result["revision"]==2
        assert client.patch(url,json=update,headers=auth).json()["todo"]==result
        assert client.patch(url,json={**update,"request_id":str(uuid4())},headers=auth).status_code==409
        assert client.get(f"/api/v1/projects/{b}").json()["tasks"]==[]
        assert len(client.get(url+"/history").json()["events"])==2
    with TestClient(create_app(core_config(world)),base_url=ORIGIN) as client:
        login(client,world)
        assert client.get(f"/api/v1/projects/{a}").json()["todos"][0]==result


def test_m6_http_auth_preview_and_memory_replacement_guards(world):
    with TestClient(create_app(core_config(world)),base_url=ORIGIN) as client:
        assert client.get(f"/api/v1/projects/{uuid4()}").status_code==401
        auth=login(client,world)
        project=client.post("/api/v1/projects",json={"name":"A"},headers=auth).json()["project"]["project_id"]
        conv=client.post("/api/v1/chat/conversations",json={"request_id":str(uuid4()),"project_id":project},headers=auth).json()["conversation"]
        path=f"/api/v1/chat/conversations/{conv['conversation_id']}/memory-preview"
        assert client.post(path,json={"memory_ids":[]}).status_code==403
        assert client.post(path,json={"memory_ids":[]},headers=auth).status_code==200
        mem={"request_id":str(uuid4()),"consent":True,"content":"A","kind":"fact","source":"user","project_id":project}
        row=client.post("/api/v1/memories",json=mem,headers=auth).json()["memory"]
        assert client.post("/api/v1/memories",json={**mem,"request_id":str(uuid4()),"project_id":None,"supersedes_id":row["memory_id"]},headers=auth).status_code==400
        assert client.get("/api/v1/memories").json()["memories"][0]["superseded_at"] is None


def test_v13_upgrade_preserves_projects_memories_grants_chat_and_tasks(world, tmp_path, monkeypatch):
    import hashlib
    import sqlite3
    from contextlib import closing
    from importlib.resources import files
    from nexus.core.storage.database import Database
    from nexus.contracts.task import SystemInfoRequest, SystemInfoArguments

    grant(world)
    chat=app_for(world,monkeypatch).state.chat
    async def seed():
        records=Records(world.db)
        project=await records.create_project(world.owner,ProjectCreate(name="historical project"))
        await records.save(world.owner,MemoryCreate(request_id=uuid4(),consent=True,content="historical consent",kind="fact",source="user",project_id=UUID(project["project_id"])))
        await world.tasks.create(world.owner_id,SystemInfoRequest(request_id=uuid4(),intent="get_system_info",arguments=SystemInfoArguments(),project_id=UUID(project["project_id"])),world.node_id)
        conv=await chat.create(world.owner,ConversationCreate(request_id=uuid4()))
        await send(chat,world,conv["conversation_id"])
    asyncio.run(seed())
    path=tmp_path/"v13.sqlite3"
    with closing(sqlite3.connect(path)) as target,closing(sqlite3.connect(world.db.path)) as source:
        target.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)")
        for resource in sorted(files("nexus.core.storage").joinpath("migrations").iterdir(),key=lambda p:p.name):
            if resource.name.endswith(".sql") and int(resource.name[:3])<=13:
                script=resource.read_text(encoding="utf-8");target.executescript(script)
                target.execute("INSERT INTO schema_migrations VALUES(?,?,?)",(int(resource.name[:3]),hashlib.sha256(script.encode()).hexdigest(),timestamp(utc_now())));target.commit()
        tables=[row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'") if row[0]!="schema_migrations"]
        columns={table:[row[1] for row in target.execute(f'PRAGMA table_info("{table}")')] for table in tables}
        before={}
        for table in tables:
            names=','.join(f'"{name}"' for name in columns[table])
            data=source.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid').fetchall()
            target.execute(f'DELETE FROM "{table}"')
            target.executemany(f'INSERT INTO "{table}"({names}) VALUES({",".join("?" for _ in columns[table])})',data)
            before[table]=data
        target.commit()
    asyncio.run(Database(path).initialize())
    backups=list(tmp_path.glob("v13.sqlite3.before-v14-*.sqlite3"));assert len(backups)==1
    with closing(sqlite3.connect(path)) as target,closing(sqlite3.connect(backups[0])) as backup:
        for table in tables:
            names=','.join(f'"{name}"' for name in columns[table])
            assert target.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid').fetchall()==before[table]
            assert backup.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid').fetchall()==before[table]
        assert target.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]==20
        assert target.execute("SELECT project_id FROM conversations").fetchone()[0] is None
        assert target.execute("SELECT memory_refs_json FROM chat_turns").fetchone()[0]=="[]"
        assert target.execute("PRAGMA foreign_key_check").fetchall()==[]
