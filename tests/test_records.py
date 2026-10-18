import asyncio
import json
import socket
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.records import MemoryCreate
from nexus.core.app import create_app
from nexus.core.memory.service import Records
from nexus.core.models.providers import DisabledDecisionModel, DisabledProvider, ModelLimits
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import Database, utc_now
from test_harness_http import login, submission
from test_node_link import ORIGIN, core_config, grant


def memory(**changes):
    value = {"request_id":str(uuid4()), "consent":True, "content":"仅供验证的偏好", "kind":"fact", "source":"测试明确输入"}
    value.update(changes)
    return value


@pytest.mark.parametrize("changes", [
    {"consent":False}, {"consent":1}, {"consent":"true"}, {"kind":"guess"},
    {"verification":"verified"}, {"verified_at":"2026-01-01T00:00:00Z"},
    {"valid_until":"2026-01-01"}, {"content":" "}, {"content":"x"*2001},
    {"status":"succeeded"},
])
def test_invalid_memory_never_creates_consent_or_entry(world, changes):
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        auth = login(client, world)
        assert client.post("/api/v1/memories", json=memory(**changes), headers=auth).status_code == 400
        assert client.get("/api/v1/memories?history=true").json()["memories"] == []
    async def check():
        async with world.db.transaction(write=False) as conn:
            assert (await conn.execute_fetchall("SELECT * FROM memory_consents")) == []
    asyncio.run(check())


def test_memory_auth_csrf_origin_and_explicit_boolean_are_required(world):
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        assert client.get("/api/v1/memories").status_code == 401
        auth = login(client, world)
        for headers in ({"origin":ORIGIN}, {**auth,"origin":"https://evil.test"}):
            assert client.post("/api/v1/memories", json=memory(), headers=headers).status_code == 403
        value = memory(); value.pop("consent")
        assert client.post("/api/v1/memories", json=value, headers=auth).status_code == 400
        assert client.post("/api/v1/memories", content=" "*16385, headers=auth).status_code == 413
    async def check():
        with pytest.raises(AccessDenied):
            await Records(world.db).save(world.node, MemoryCreate.model_validate_json(json.dumps(memory())))
    asyncio.run(check())


def test_memory_project_evidence_supersession_expiry_retraction_and_reopen(world):
    grant(world)
    app = create_app(core_config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        auth = login(client, world)
        project = client.post("/api/v1/projects", json={"name":"演示项目","description":"测试"}, headers=auth).json()["project"]
        project_id = project["project_id"]
        value = memory(project_id=project_id, verification="verified", evidence_ref="user:test-assertion", verified_at=(utc_now()-timedelta(minutes=1)).isoformat())
        first = client.post("/api/v1/memories", json=value, headers=auth).json()["memory"]
        again = client.post("/api/v1/memories", json=value, headers=auth).json()["memory"]
        assert again == first and first["consent_event_id"] and first["verified_at"]
        assert client.post("/api/v1/memories", json={**value,"content":"changed"}, headers=auth).status_code == 409
        expired = client.post("/api/v1/memories", json=memory(kind="plan", valid_until=(utc_now()-timedelta(seconds=1)).isoformat()), headers=auth).json()["memory"]
        second = client.post("/api/v1/memories", json=memory(kind="inference", supersedes_id=first["memory_id"], project_id=project_id), headers=auth).json()["memory"]
        active = client.get("/api/v1/memories").json()["memories"]
        assert [m["memory_id"] for m in active] == [second["memory_id"]]
        historical = client.get("/api/v1/memories?history=true").json()["memories"]
        assert len(historical) == 3 and historical[0]["superseded_at"]
        result = client.post(f"/api/v1/memories/{second['memory_id']}/retract", json={}, headers=auth).json()["memory"]
        assert result["retracted_at"]
        assert client.post(f"/api/v1/memories/{second['memory_id']}/retract", json={}, headers=auth).json()["memory"] == result
        assert client.get("/api/v1/memories").json()["memories"] == []
        assert len(client.get(f"/api/v1/memories?history=true&project_id={project_id}").json()["memories"]) == 2
        req = submission(world); req["request"]["project_id"] = project_id
        task = client.post("/api/v1/tasks", json=req, headers=auth).json()["task"]
        assert task["project_id"] == project_id
        assert client.post("/api/v1/tasks", json=req, headers=auth).json()["task"]["task_id"] == task["task_id"]
        routes = client.get("/api/v1/records/routes").json()["routes"]
        assert len(routes) == 1 and routes[0]["reason_code"] == "KNOWN_SYSTEM_INFO"
        assert routes[0]["origin"] == "live" and routes[0]["elapsed_ms"] >= 0
        assert routes[0]["project_id"] == project_id
        usage = client.get("/api/v1/models/usage").json()
        assert usage == {"enabled":False,"calls":0,"cost_microunits":0,"unknown_cost_calls":0,"model_success_rate":None}
    # Reconstruct the app/database rather than retaining in-memory repositories.
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        login(client, world)
        assert client.get("/api/v1/memories?history=true").json()["memories"][-1] == result
        assert client.get("/api/v1/records/projects").json()["projects"] == [project]
        assert client.get("/api/v1/records/routes").json()["routes"] == routes
        assert expired["valid_until"]


def test_memory_atomic_consent_failure_and_project_bounds(world):
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        auth = login(client, world)
        assert client.post("/api/v1/memories", json=memory(project_id=str(uuid4())), headers=auth).status_code == 404
        assert client.post("/api/v1/memories", json=memory(supersedes_id=str(uuid4())), headers=auth).status_code == 404
        assert client.post("/api/v1/memories", json=memory(verification="verified", evidence_ref="test", verified_at=(utc_now()+timedelta(days=1)).isoformat()), headers=auth).status_code == 400
        assert client.get("/api/v1/memories?limit=101").status_code == 422
        assert client.get("/api/v1/records/routes?after=-1").status_code == 422
        async def fail():
            async with world.db.transaction() as conn:
                await conn.execute("CREATE TRIGGER injected BEFORE INSERT ON memory_entries BEGIN SELECT RAISE(ABORT,'injected'); END")
        asyncio.run(fail())
        assert client.post("/api/v1/memories", json=memory(), headers=auth).status_code == 503
        assert client.app.state.harness.failed
    async def check():
        async with world.db.transaction(write=False) as conn:
            assert await conn.execute_fetchall("SELECT * FROM memory_consents") == []
            assert await conn.execute_fetchall("SELECT * FROM memory_entries") == []
    asyncio.run(check())


def test_disabled_models_make_no_network_or_call_rows(world, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled models attempted a network operation")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    async def check():
        provider = DisabledProvider()
        with pytest.raises(ProtocolError) as exc:
            provider.estimate("anything")
        assert exc.value.error.code == ErrorCode.MODEL_DISABLED
        for call in (provider.invoke("anything", ModelLimits()), DisabledDecisionModel().decide("anything", ModelLimits())):
            with pytest.raises(ProtocolError) as exc:
                await call
            assert exc.value.error.code == ErrorCode.MODEL_DISABLED
        async with world.db.transaction(write=False) as conn:
            assert await conn.execute_fetchall("SELECT * FROM model_calls") == []
    asyncio.run(check())


def test_v4_upgrade_preserves_records_backfills_routes_and_legacy_idempotency(world, tmp_path):
    import hashlib
    import sqlite3
    from contextlib import closing
    from importlib.resources import files
    from nexus.core.storage.database import timestamp
    from nexus.core.storage.tasks import TaskRepository
    from nexus.contracts.task import SystemInfoRequest
    grant(world)
    request = SystemInfoRequest.model_validate_json(json.dumps(submission(world)["request"]))
    original = asyncio.run(world.tasks.create(world.owner_id, request, world.node_id))
    path = tmp_path / "v4.sqlite3"
    with closing(sqlite3.connect(path)) as target, closing(sqlite3.connect(world.db.path)) as source, target:
        target.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)")
        for name in ("001_tasks.sql","002_security.sql","003_execution.sql","004_recovery.sql"):
            script = files("nexus.core.storage").joinpath("migrations/"+name).read_text(encoding="utf-8")
            target.executescript(script)
            target.execute("INSERT INTO schema_migrations VALUES(?,?,?)",(int(name[:3]),hashlib.sha256(script.encode()).hexdigest(),timestamp(utc_now())))
        tables = [r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0] not in {"schema_migrations","sqlite_sequence"}]
        for table in tables:
            columns = ",".join(r[1] for r in target.execute(f"PRAGMA table_info({table})"))
            rows = source.execute(f"SELECT {columns} FROM {table}").fetchall()
            target.execute(f"DELETE FROM {table}")
            target.executemany(f"INSERT INTO {table}({columns}) VALUES({','.join('?' for _ in columns.split(','))})",rows)
        legacy = request.model_dump(mode="json"); legacy.pop("project_id")
        target.execute("UPDATE tasks SET request_json=?",(json.dumps(legacy),))
        before = {table:target.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    db = Database(path)
    asyncio.run(db.initialize())
    duplicate = asyncio.run(TaskRepository(db).create(world.owner_id, request, world.node_id))
    assert duplicate.task_id == original.task_id
    backups = list(tmp_path.glob("v4.sqlite3.before-v5-*.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(path)) as target, closing(sqlite3.connect(backups[0])) as backup:
        for table, rows in before.items():
            assert backup.execute(f"SELECT * FROM {table}").fetchall() == rows
            if table != "tasks": assert target.execute(f"SELECT * FROM {table}").fetchall() == rows
        assert target.execute("SELECT reason_code,origin,elapsed_ms FROM routing_decisions").fetchone() == ("KNOWN_SYSTEM_INFO","migrated",None)
        assert target.execute("SELECT count(*) FROM client_events").fetchone()[0] == len(before["task_events"]) + len(before["control_events"])
        assert target.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert target.execute("PRAGMA foreign_key_check").fetchall() == []
