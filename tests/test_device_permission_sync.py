"""Owner-approved local choices, durable retries and live permission intersection."""
import asyncio
import hashlib
import sqlite3
from importlib.resources import files
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nexus.contracts.messages import NodeHello
from nexus.contracts.protocol import parse_message
from nexus.core.app import create_app
from nexus.core.security.devices import DevicePermissionSync, TOOLS
from nexus.core.security.service import has_grant
from nexus.core.security.types import AccessDenied
from nexus.core.storage.database import Database, one
from nexus.core.storage.tasks import Conflict
from nexus.link import wire_message
from test_device_enrollment import binding, say
from test_harness_http import login
from test_node_link import ORIGIN, core_config


def test_local_choices_are_scoped_durable_and_do_not_restore_remote_revocation(world):
    async def run():
        chat = create_app(core_config(world)).state.chat
        device = binding()
        await chat.devices.prepare(world.owner, device)
        await say(chat, world, "将此设备注册为节点，备注为测试设备", device)
        async def sync(revision, **permissions):
            return await chat.devices.sync_permissions(world.owner, device.client_id,
                device.proof.get_secret_value(), DevicePermissionSync(revision=revision, permissions=permissions))
        first = await sync(1, accessibility=True, files=True, vision=False)
        assert first["permissions"]["accessibility"] and first["permissions"]["files"]
        assert not first["permissions"]["vision"]
        async with world.db.transaction(write=False) as conn:
            assert await has_grant(conn, world.owner_id, device.node_id, "android.ui_act", 1, "android:ui")
            assert not await has_grant(conn, world.owner_id, world.node_id, "android.ui_act", 1, "android:ui")
            version = (await one(conn, "SELECT version FROM policy_state"))[0]
        assert await sync(1, accessibility=True, files=True, vision=False) == first
        async with world.db.transaction(write=False) as conn:
            assert (await one(conn, "SELECT version FROM policy_state"))[0] == version
            assert (await one(conn, "SELECT count(*) FROM audit_events WHERE kind='device.permissions_synced'"))[0] == 1
        await say(chat, world, "关闭此设备的无障碍操作权限", device)
        # A lost successful reply followed by a retry cannot undo a later revocation.
        assert not (await sync(1, accessibility=True, files=True, vision=False))["permissions"]["accessibility"]
        assert not (await sync(2, accessibility=True, files=True, vision=True))["permissions"]["accessibility"]
        restarted = create_app(core_config(world)).state.chat
        await restarted.devices.reload()
        state = await restarted.devices.status(world.owner, device.client_id, device.proof.get_secret_value())
        assert state["permission_sync"]["revision"] == 2 and state["permissions"]["vision"]
        with pytest.raises(Conflict):
            await sync(1, accessibility=True)
        with pytest.raises(Conflict):
            await sync(2, accessibility=False, files=True, vision=True)
        await sync(3, accessibility=False, files=False, vision=False)
        enabled = await sync(4, accessibility=True, files=False, vision=False)
        assert enabled["permissions"]["accessibility"] and not enabled["permissions"]["files"]
        async with world.db.transaction(write=False) as conn:
            assert not await has_grant(conn, world.owner_id, device.node_id, "android.file_write", 1, "android:files")
            assert not await has_grant(conn, device.node_id, device.node_id, "android.file_write", 1, "android:files")
            assert not await one(conn, "SELECT 1 FROM model_calls")
    asyncio.run(run())


def test_http_sync_requires_owner_csrf_and_bound_device_and_updates_live_scope(world):
    app = create_app(core_config(world))
    device = binding()
    async def setup():
        await app.state.chat.devices.prepare(world.owner, device)
        await say(app.state.chat, world, "将此设备注册为节点", device)
    asyncio.run(setup())
    path = f"/api/v1/device-clients/{device.client_id}/permissions"
    body = dict(revision=1, permissions=dict(accessibility=True, files=True, vision=True))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.put(path, json=body, headers={"origin": ORIGIN}).status_code in {401, 403}
        auth = login(client, world)
        proof = {**auth, "X-Nexus-Device-Proof": device.proof.get_secret_value()}
        assert client.put(path, json=body, headers=auth).status_code == 401
        assert client.put(path, json=body, headers={"origin": ORIGIN, "X-Nexus-Device-Proof": device.proof.get_secret_value()}).status_code == 403
        assert client.put(path, json=body, headers={**proof, "X-Nexus-Device-Proof": device.node_token.get_secret_value()}).status_code == 401
        assert client.put(path.replace(str(device.client_id), str(uuid4())), json=body, headers=proof).status_code == 401
        for bad in ({**body, "permissions": {"shell": True}}, {**body, "permissions": {"files": "true"}}, {**body, "revision": True}):
            assert client.put(path, json=bad, headers=proof).status_code == 400
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers={"authorization": "Bearer " + device.node_token.get_secret_value()}) as ws:
            ws.send_text(wire_message("node.hello", NodeHello(node_id=device.node_id, node_version="sync-test", boot_id=uuid4(),
                tools=[{"tool": tool, "tool_version": 1} for caps in TOOLS.values() for tool, _ in caps],
                roots=["android:ui", "android:device", "android:files", "android:notifications", "workspace"])))
            assert parse_message(ws.receive_text()).type == "node.welcome"
            def node():
                return next(n for n in client.get("/api/v1/nodes").json()["nodes"] if n["node_id"] == str(device.node_id))
            assert node()["tools"] == ["android.device_status@1"]
            response = client.put(path, json=body, headers=proof)
            assert response.status_code == 200, response.text
            assert {"android.ui_act@1", "android.file_write@1", "android.screen_act@1"} <= set(node()["tools"])
            assert client.put(path, json=dict(revision=2, permissions=dict(accessibility=False, files=False, vision=False)), headers=proof).status_code == 200
            assert node()["tools"] == ["android.device_status@1"]
            assert client.put(path, json=body, headers=proof).status_code == 409


@pytest.mark.parametrize("kind", ["unregistered", "revoked", "rotated", "windows"])
def test_sync_cannot_create_or_revive_identity_or_grant_windows_tools(world, kind):
    async def run():
        chat = create_app(core_config(world)).state.chat
        device = binding()
        if kind == "windows":
            device = device.model_copy(update={"node_id": world.node_id, "node_token": world.node_token})
        await chat.devices.prepare(world.owner, device)
        if kind in {"revoked", "rotated"}:
            await say(chat, world, "将此设备注册为节点", device)
            async with world.db.transaction() as conn:
                if kind == "revoked":
                    await conn.execute("UPDATE identities SET revoked_at='2026-10-26T00:00:00Z' WHERE identity_id=?", (str(device.node_id),))
                else:
                    await conn.execute("UPDATE identities SET token_hash=? WHERE identity_id=?", ("0" * 64, str(device.node_id)))
        with pytest.raises(AccessDenied):
            await chat.devices.sync_permissions(world.owner, device.client_id, device.proof.get_secret_value(), DevicePermissionSync(revision=1, permissions={"files": True}))
        with pytest.raises(AccessDenied):
            await chat.devices.sync_permissions(world.node, device.client_id, device.proof.get_secret_value(), DevicePermissionSync(revision=1, permissions={"files": True}))
    asyncio.run(run())


@pytest.mark.parametrize("revision", [0, -1, 2**63, True, "1"])
def test_sync_rejects_invalid_revision(revision):
    with pytest.raises(ValidationError):
        DevicePermissionSync(revision=revision, permissions={"files": True})


def test_044_migration_preserves_043_database_and_creates_no_permissions(tmp_path):
    path = tmp_path / "core.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)")
        for script in sorted(files("nexus.core.storage").joinpath("migrations").iterdir(), key=lambda p: p.name):
            if script.name.endswith(".sql") and int(script.name[:3]) <= 43:
                source = script.read_text(encoding="utf-8")
                conn.executescript(source)
                conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (int(script.name[:3]), hashlib.sha256(source.encode()).hexdigest(), "2026-10-26T00:00:00Z"))
        owner, node = str(uuid4()), str(uuid4())
        conn.execute("INSERT INTO identities VALUES(?,'owner',?,NULL,?)", (owner, "a" * 64, "2026-10-26T00:00:00Z"))
        conn.execute("INSERT INTO identities VALUES(?,'node',?,NULL,?)", (node, "b" * 64, "2026-10-26T00:00:00Z"))
        conn.execute("INSERT INTO device_clients VALUES(?,?,?,?,?,?,?)", (str(uuid4()), owner, node, "c" * 64, "b" * 64, "旧设备", "2026-10-26T00:00:00Z"))
        conn.execute("INSERT INTO managed_node_profiles(node_id,description) VALUES(?,?)", (node, "保留"))
        conn.execute("INSERT INTO device_owner_grants VALUES(?,?,?,1,?)", (owner, node, "android.ui_act", "android:ui"))
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_migrations'")]
        before = {t: list(conn.execute(f'SELECT * FROM "{t}"')) for t in tables}
    asyncio.run(Database(path).initialize())
    with sqlite3.connect(path) as conn:
        assert before == {t: list(conn.execute(f'SELECT * FROM "{t}"')) for t in tables}
        assert conn.execute("SELECT count(*) FROM device_permission_sync").fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not list(conn.execute("PRAGMA foreign_key_check"))
    assert list(tmp_path.glob("*.before-v44-*.sqlite3"))
