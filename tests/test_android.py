"""Android extensions through real Core authorization, migration and v1 Link."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from nexus.contracts.android import AndroidDeviceStatusCall, AndroidDeviceStatusResult, AndroidNotificationDigestCall, AndroidNotificationDigestResult
from nexus.contracts.base import MAX_RESULT_BYTES
from nexus.contracts.commands import command_digest
from nexus.contracts.messages import CommandExecute, CommandResult, NodeHello
from nexus.contracts.protocol import parse_message
from nexus.contracts.tools import validate_result
from nexus.core.app import create_app
from nexus.core.config import TrustedNode
from nexus.core.security.types import NodeScope
from nexus.link import wire_message
from test_node_link import core_config, headers
from test_harness_http import login

DEVICE = "android.device_status"
NOTIFICATIONS = "android.notification_digest"
RESOURCES = {DEVICE: "android:device", NOTIFICATIONS: "android:notifications"}


def device_result():
    return {"android_api": 36, "app_version": "0.1.0", "battery": {"level_percent": 71, "charging": False, "plugged": "none"},
            "network": {"connected": True, "transport": "wifi", "metered": False},
            "screen": {"interactive": True, "locked": False}, "storage": {"free_bytes": 500, "total_bytes": 1000}}


def setup(world, tool=DEVICE, both=True):
    async def grant():
        for identity in ((world.owner_id,world.node_id) if both else (world.node_id,)):
            await world.security.set_grant(identity,tool,root_id=RESOURCES[tool])
    asyncio.run(grant())
    return core_config(world,nodes={world.node_id: TrustedNode(tools=frozenset({tool+"@1"}),roots=frozenset({RESOURCES[tool]}))})


def submit(world, tool=DEVICE):
    return {"node_id": str(world.node_id), "request": {"request_id": str(uuid4()), "intent": tool.replace(".","_"), "arguments": {} if tool == DEVICE else {"limit": 2}}}


@pytest.mark.parametrize("tool", [DEVICE,NOTIFICATIONS])
def test_android_roundtrip_durable_dispatch_result_and_ack(world, tool):
    app = create_app(setup(world,tool))
    with TestClient(app,base_url="http://127.0.0.1:8000") as client:
        auth=login(client,world)
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node",headers=headers(world)) as ws:
            hello=NodeHello(node_id=world.node_id,node_version="android-0.1.0",tools=[{"tool":tool,"tool_version":1}],roots=[RESOURCES[tool]],boot_id=uuid4())
            ws.send_text(wire_message("node.hello",hello))
            welcome=parse_message(ws.receive_text())
            session=welcome.payload.session_id
            request=submit(world,tool)
            task=client.post("/api/v1/tasks",json=request,headers=auth).json()["task"]
            if tool == NOTIFICATIONS:
                assert task["status"] == "awaiting_approval"
                detail=client.get("/api/v1/tasks/"+task["task_id"]).json()
                assert not detail["attempts"]
                client.post("/api/v1/approvals/"+detail["approvals"][0]["approval_id"],json={"decision":"approve"},headers=auth).raise_for_status()
            command=parse_message(ws.receive_text()).payload
            assert command_digest(command,world.node_id)==command.command_digest
            detail=client.get("/api/v1/tasks/"+task["task_id"]).json()
            assert len(detail["attempts"])==1
            result=TypeAdapter(CommandResult).validate_json(json.dumps({
                "session_id":str(session),"task_id":str(command.task_id),"attempt_id":str(command.attempt_id),
                "tool":tool,"tool_version":1,"command_digest":command.command_digest,"observed_at":datetime.now(UTC).isoformat(),
                "outcome":"succeeded","result":device_result() if tool==DEVICE else {"entries":[],"truncated":False}}))
            ws.send_text(wire_message("command.result",result))
            assert parse_message(ws.receive_text()).type=="result.ack"
            detail=client.get("/api/v1/tasks/"+task["task_id"]).json()
            assert detail["task"]["status"]=="succeeded"
            assert detail["task"]["verification"]=="node_report_schema_checked"
            repeated=client.post("/api/v1/tasks",json=request,headers=auth).json()["task"]
            assert repeated["task_id"]==task["task_id"]


@pytest.mark.parametrize("both", [False,True])
def test_android_requires_both_grants_and_exact_resources(world,both):
    config=setup(world,both=both)
    with TestClient(create_app(config),base_url="http://127.0.0.1:8000") as client:
        auth=login(client,world)
        task=client.post("/api/v1/tasks",json=submit(world),headers=auth).json()["task"]
        assert task["status"]==("ready" if both else "failed")
    async def check():
        with pytest.raises(ValueError):
            await world.security.set_grant(world.node_id,DEVICE,root_id="workspace")
        for local_roots in (frozenset(),frozenset({"workspace"})):
            scope=NodeScope(world.node_id,frozenset({DEVICE+"@1"}),frozenset({"android:device"}),frozenset({DEVICE+"@1"}),local_roots)
            assert not (await world.security.connection_scope(world.node,world.owner_id,scope)).tools
    asyncio.run(check())


@pytest.mark.parametrize("field,value", [("android_api",25),("app_version",""),("extra",True)])
def test_android_rejects_invalid_device_fields(field,value):
    data=device_result(); data[field]=value
    with pytest.raises(ValidationError): AndroidDeviceStatusResult.model_validate(data)


def test_android_storage_network_notification_boundaries():
    data=device_result(); data["storage"]["free_bytes"]=1001
    with pytest.raises(ValidationError): AndroidDeviceStatusResult.model_validate(data)
    data=device_result(); data["network"]["transport"]="none"
    with pytest.raises(ValidationError): AndroidDeviceStatusResult.model_validate(data)
    entry={"key":"a"*64,"package_name":"test.example","title":"测试","text":"摘要","posted_at":datetime.now(UTC).isoformat(),"ongoing":False,"redacted":False}
    with pytest.raises(ValidationError): AndroidNotificationDigestResult.model_validate({"entries":[entry,entry],"truncated":False})
    result=AndroidNotificationDigestResult.model_validate({"entries":[entry,{**entry,"key":"b"*64}],"truncated":False})
    call=AndroidNotificationDigestCall(tool=NOTIFICATIONS,tool_version=1,arguments={"limit":1})
    with pytest.raises(ValueError): validate_result(call,result,MAX_RESULT_BYTES)


def test_android_future_notification_rejected():
    now=datetime.now(UTC)
    entry={"key":"a"*64,"package_name":"test.example","title":"","text":"","posted_at":(now+timedelta(seconds=6)).isoformat(),"ongoing":False,"redacted":False}
    with pytest.raises(ValidationError):
        TypeAdapter(CommandResult).validate_json(json.dumps({"session_id":str(uuid4()),"task_id":str(uuid4()),"attempt_id":str(uuid4()),"command_digest":"b"*64,
            "tool":NOTIFICATIONS,"tool_version":1,"observed_at":now.isoformat(),"outcome":"succeeded","result":{"entries":[entry],"truncated":False}}))


def test_android_origin_is_explicit_and_csrf_still_required(world):
    config=setup(world).model_copy(update={"origins":frozenset({"https://app.nexus.local"})})
    with TestClient(create_app(config),base_url="http://127.0.0.1:8000") as client:
        assert client.post("/api/v1/session",json={"token":world.owner_token.get_secret_value()},headers={"origin":"https://wrong.test"}).status_code==403
        response=client.post("/api/v1/session",json={"token":world.owner_token.get_secret_value()},headers={"origin":"https://app.nexus.local"})
        assert response.status_code==200
        assert client.post("/api/v1/tasks",json=submit(world),headers={"origin":"https://app.nexus.local"}).status_code==403
        assert client.post("/api/v1/tasks",json=submit(world),headers={"origin":"https://app.nexus.local","x-csrf-token":response.json()["csrf_token"]}).status_code==200


def test_android_python_kotlin_golden_vectors():
    from pathlib import Path
    from uuid import UUID
    from nexus.contracts.commands import command_identity
    vectors=json.loads((Path(__file__).parents[1]/"apps/android/protocol/src/test/resources/commands.json").read_text(encoding="utf-8"))
    for vector in vectors:
        command=parse_message(vector["wire"]).payload
        assert command_digest(command,UUID(vector["node_id"]))==command.command_digest
        assert command_identity(command,UUID(vector["node_id"]))==vector["identity"]


def test_android_migration_preserves_existing_grants_and_backup(tmp_path):
    import hashlib
    import sqlite3
    from pathlib import Path
    from nexus.core.storage.database import Database
    path=tmp_path/"core.sqlite3"
    scripts=sorted((Path(__file__).parents[1]/"src/nexus/core/storage/migrations").glob("*.sql"))
    identity,grant=str(uuid4()),str(uuid4())
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT,applied_at TEXT)")
        for script in scripts:
            if int(script.name[:3])>=31: break
            text=script.read_text(encoding="utf-8")
            conn.executescript(text)
            conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)",(int(script.name[:3]),hashlib.sha256(text.encode()).hexdigest(),"2026-10-23T00:00:00+00:00"))
            conn.commit()
        conn.execute("INSERT INTO identities VALUES(?,'owner','fixture-digest',NULL,?)",(identity,"2026-10-23T00:00:00+00:00"))
        conn.execute("INSERT INTO grants VALUES(?,?,'system.info',1,'')",(grant,identity))
    asyncio.run(Database(path).initialize())
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM grants").fetchall()==[(grant,identity,"system.info",1,"")]
        assert conn.execute("PRAGMA foreign_key_check").fetchall()==[]
        with pytest.raises(sqlite3.IntegrityError): conn.execute("INSERT INTO grants VALUES(?,?,'android.device_status',1,'workspace')",(str(uuid4()),identity))
    backup=list(tmp_path.glob("*.before-v31-*.sqlite3"))
    assert len(backup)==1
    with sqlite3.connect(backup[0]) as conn:
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]==30
        assert conn.execute("SELECT * FROM grants").fetchall()==[(grant,identity,"system.info",1,"")]


def test_android_catalog_follows_live_capabilities(world):
    app=create_app(setup(world,NOTIFICATIONS))
    with TestClient(app,base_url="http://127.0.0.1:8000") as client:
        login(client,world)
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node",headers=headers(world)) as ws:
            ws.send_text(wire_message("node.hello",NodeHello(node_id=world.node_id,node_version="android-test",boot_id=uuid4(),tools=[{"tool":NOTIFICATIONS,"tool_version":1}],roots=[RESOURCES[NOTIFICATIONS]])))
            ws.receive_text()
            response=client.get("/api/v1/chat/work-catalog",params={"node_id":str(world.node_id)})
            response.raise_for_status()
            names={entry["name"] for entry in response.json()["catalog"]}
            assert "android_notification_digest" in names
            assert "android_device_status" not in names
            assert "read_file" not in names
