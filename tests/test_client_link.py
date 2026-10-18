import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nexus.contracts.messages import ClientSync, ClientSynced, ControlRequest, TaskCancel
from nexus.contracts.protocol import parse_message
from nexus.core.app import create_app
from nexus.core.config import TrustedNode
from nexus.core.link.clients import BrowserConnection, SlowClient
from nexus.core.storage.client_events import ClientEvents
from nexus.link import wire_message
from test_harness_http import login, submission
from test_node_link import ORIGIN, core_config, grant

URL = "ws://127.0.0.1:8000/ws/v1/client"


def sync(ws, cursor=None):
    ws.send_text(wire_message("client.sync", ClientSync(event_id=cursor)))
    value = parse_message(ws.receive_text())
    assert value.type == "client.synced"
    return value.payload


def until(ws, kind, predicate=lambda p: True):
    for _ in range(100):
        value = parse_message(ws.receive_text())
        if value.type == "error":
            pytest.fail(str(value))
        if value.type == kind and predicate(value.payload):
            return value.payload
    pytest.fail("expected event not found in bounded replay")


@pytest.mark.parametrize("kind", ["missing", "origin", "node_cookie", "direction", "invalid_node"])
def test_browser_handshake_rejects_bad_identity_origin_and_direction(world, kind):
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        if kind != "missing": login(client, world)
        if kind == "node_cookie": client.cookies.set("nexus_session", world.node_token.get_secret_value())
        headers = {"origin":"https://evil.test" if kind == "origin" else ORIGIN}
        url = URL + ("?node_id="+str(uuid4()) if kind == "invalid_node" else "")
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(url, headers=headers) as ws:
                if kind == "direction": ws.send_text(wire_message("control.pause", ControlRequest(request_id=uuid4())))
                else: ws.send_text(wire_message("client.sync", ClientSync(event_id=None)))
                ws.receive_text()
        assert exc.value.code == 1008


def test_ws_submission_idempotency_control_cancel_and_replay_after_restart(world):
    grant(world)
    cfg = core_config(world)
    with TestClient(create_app(cfg), base_url=ORIGIN) as client:
        login(client, world)
        with client.websocket_connect(URL, headers={"origin":ORIGIN}) as ws:
            assert sync(ws).full_sync_required
            request = submission(world)["request"]
            from nexus.contracts.task import SystemInfoRequest
            payload = SystemInfoRequest.model_validate_json(json.dumps(request))
            ws.send_text(wire_message("task.submit", payload))
            task = until(ws, "task.accepted")
            updated = until(ws, "task.updated", lambda p:p.status == "ready")
            cursor = updated.event_id
            ws.send_text(wire_message("task.submit", payload))
            assert until(ws, "task.accepted").task_id == task.task_id
            ws.send_text(wire_message("control.pause", ControlRequest(request_id=uuid4())))
            paused = until(ws, "control.updated")
            assert paused.paused and int(paused.event_id) > int(cursor)
            ws.send_text(wire_message("task.cancel", TaskCancel(task_id=task.task_id)))
            cancelled = until(ws, "task.updated", lambda p:p.status == "cancelled")
            assert int(cancelled.event_id) > int(paused.event_id)
            assert client.get("/api/v1/client/snapshot").json()["tasks"][0]["status"] == "cancelled"
    with TestClient(create_app(cfg), base_url=ORIGIN) as client:
        login(client, world)
        with client.websocket_connect(URL, headers={"origin":ORIGIN}) as ws:
            assert not sync(ws, cursor).full_sync_required
            assert until(ws, "control.updated").paused
            assert until(ws, "task.updated", lambda p:p.status == "cancelled").task_id == task.task_id
        with client.websocket_connect(URL, headers={"origin":ORIGIN}) as ws:
            assert sync(ws, "9223372036854775808").full_sync_required


def test_ws_directory_approval_and_invalid_messages(world):
    grant(world, "filesystem.list")
    cfg = core_config(world, nodes={world.node_id:TrustedNode(tools=frozenset({"filesystem.list@1"}), roots=frozenset({"workspace"}))})
    with TestClient(create_app(cfg), base_url=ORIGIN) as client:
        login(client, world)
        with client.websocket_connect(URL, headers={"origin":ORIGIN}) as ws:
            sync(ws)
            ws.send_text("not json")
            assert parse_message(ws.receive_text()).payload.code == "INVALID_MESSAGE"
            from nexus.contracts.task import ListDirectoryRequest
            from nexus.contracts.messages import ApprovalDecide
            payload = ListDirectoryRequest.model_validate_json(json.dumps(submission(world, directory=True)["request"]))
            ws.send_text(wire_message("task.submit", payload))
            task = until(ws, "task.accepted")
            approval = until(ws, "approval.required")
            assert approval.task_id == task.task_id and approval.action.arguments.root_id == "workspace"
            ws.send_text(wire_message("approval.decide", ApprovalDecide(approval_id=approval.approval_id, decision="approve")))
            assert until(ws, "task.updated", lambda p:p.status == "ready").task_id == task.task_id
            ws.send_text(wire_message("control.cancel_all", ControlRequest(request_id=uuid4())))
            assert until(ws, "task.updated", lambda p:p.status == "cancelled").task_id == task.task_id


def test_expired_idle_browser_closed_and_http_session_restored(world):
    app = create_app(core_config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        auth = login(client, world)
        restored = client.get("/api/v1/session")
        assert restored.status_code == 200 and restored.json()["csrf_token"] == auth["x-csrf-token"]
        assert "no-store" in restored.headers["cache-control"]
        with client.websocket_connect(URL, headers={"origin":ORIGIN}) as ws:
            sync(ws)
            client.delete("/api/v1/session", headers=auth)
            with pytest.raises(WebSocketDisconnect) as exc: ws.receive_text()
            assert exc.value.code == 1008


def test_slow_browser_queue_and_send_timeout_do_not_block_harness(world):
    class BlockedSocket:
        async def send_text(self, raw): await asyncio.Event().wait()
    app = create_app(core_config(world))
    # The same production sender and queue are exercised with a blocked transport.
    async def exercise():
        link = app.state.client_link
        connection = BrowserConnection(BlockedSocket(), world.owner, world.node_id)
        payload = ClientSynced(event_id="0", full_sync_required=True)
        for _ in range(32): connection.enqueue("client.synced", payload)
        with pytest.raises(SlowClient): connection.enqueue("client.synced", payload)
        sender = asyncio.create_task(link.sender(connection))
        async with asyncio.timeout(1): await app.state.harness.tick()
        with pytest.raises(SlowClient): await sender
    asyncio.run(exercise())


def test_event_filter_boundary_and_snapshot_page(world):
    grant(world)
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        auth = login(client, world)
        start, _ = asyncio.run(ClientEvents(world.db).sync(world.owner_id, None))
        task = client.post("/api/v1/tasks", json=submission(world), headers=auth).json()["task"]
        events, end = asyncio.run(ClientEvents(world.db).page(world.owner_id, start))
        assert events and end > start and all(str(payload.task_id) == task["task_id"] for _,payload in events)
        events, end = asyncio.run(ClientEvents(world.db).page(uuid4(), start))
        assert not events and end == start
        assert client.get("/api/v1/client/snapshot?before=0").status_code == 422


def test_bundled_static_page_and_security_headers(world):
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        page = client.get("/")
        assert page.status_code == 200 and "NEXUS" in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["x-content-type-options"] == "nosniff"
        js = client.get("/static/app.js")
        assert js.status_code == 200
        assert ".innerHTML" not in js.text and "localStorage" not in js.text
        assert client.get("/static/app.css").status_code == 200


@pytest.mark.parametrize("kind", ["direction", "binary", "oversized", "rate"])
def test_running_browser_rejects_invalid_frames_and_rate(world, kind):
    app = create_app(core_config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, world)
        with client.websocket_connect(URL, headers={"origin":ORIGIN}) as ws:
            sync(ws)
            if kind == "binary": ws.send_bytes(b"{}")
            elif kind == "direction": ws.send_text(wire_message("client.synced", ClientSynced(event_id="0",full_sync_required=False)))
            elif kind == "oversized": ws.send_text(" "*65537)
            else:
                for _ in range(60):
                    ws.send_text("{}")
                    assert parse_message(ws.receive_text()).payload.code == "INVALID_MESSAGE"
                ws.send_text("{}")
            if kind == "oversized":
                assert parse_message(ws.receive_text()).payload.code == "INVALID_MESSAGE"
            else:
                with pytest.raises(WebSocketDisconnect) as exc:
                    response = ws.receive_text()
                    assert kind == "rate" and parse_message(response).payload.code == "BUSY"
                    ws.receive_text()
                assert exc.value.code == (1013 if kind == "rate" else 1008)
        assert client.get("/api/v1/tasks").json()["tasks"] == []


def test_stream_write_failure_rolls_back_task_route_events_and_audit(world):
    from nexus.contracts.task import SystemInfoArguments, SystemInfoRequest
    async def check():
        async with world.db.transaction() as conn:
            await conn.execute("CREATE TRIGGER injected BEFORE INSERT ON client_events BEGIN SELECT RAISE(ABORT,'injected'); END")
        request = SystemInfoRequest(request_id=uuid4(),intent="get_system_info",arguments=SystemInfoArguments())
        with pytest.raises(sqlite3.IntegrityError): await world.tasks.create(world.owner_id, request, world.node_id)
        async with world.db.transaction(write=False) as conn:
            for table in ("tasks","task_events","routing_decisions","client_events"):
                assert await conn.execute_fetchall(f"SELECT * FROM {table}") == []
            assert await conn.execute_fetchall("SELECT * FROM audit_events WHERE kind='task.created'") == []
    asyncio.run(check())
