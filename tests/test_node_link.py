import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.messages import CommandStatus, NodeHeartbeat, NodeHello, ToolCapability
from nexus.contracts.protocol import parse_message
from nexus.core.app import create_app
from nexus.core.config import CoreConfig, TrustedNode
from nexus.link import MessageRate, wire_message

ORIGIN = "http://127.0.0.1:8000"


def core_config(world, **changes):
    values = dict(data_dir=world.db.path.parent, owner_id=world.owner_id,
                  local_http=True, origins=frozenset({ORIGIN}),
                  heartbeat_interval_seconds=1, heartbeat_timeout_seconds=2,
                  nodes={world.node_id: TrustedNode(tools=frozenset({"system.info@1"}))})
    values.update(changes)
    return CoreConfig(**values)


def hello(world, **changes):
    values = dict(node_id=world.node_id, node_version="test", boot_id=uuid4(),
                  tools=[ToolCapability(tool="system.info", tool_version=1), ToolCapability(tool="filesystem.list", tool_version=1)],
                  roots=["workspace"])
    values.update(changes)
    return NodeHello(**values)


def headers(world):
    return {"authorization": "Bearer " + world.node_token.get_secret_value()}


def handshake(ws, world):
    ws.send_text(wire_message("node.hello", hello(world)))
    welcome = parse_message(ws.receive_text())
    assert welcome.type == "node.welcome"
    return welcome.payload.session_id


def login(client, world):
    assert client.post("/api/v1/session", json={"token": world.owner_token.get_secret_value()}, headers={"origin": ORIGIN}).status_code == 200


def grant(world, tool="system.info", *, enabled=True, owner=True):
    async def setup():
        for identity in ([world.owner_id, world.node_id] if owner else [world.node_id]):
            await world.security.set_grant(identity, tool, root_id="workspace" if tool == "filesystem.list" else "", enabled=enabled)
    asyncio.run(setup())


@pytest.mark.parametrize("kind", ["missing", "bad", "owner", "forged_id", "wrong_type", "unconfigured", "fake_tool"])
def test_handshake_auth_identity_and_direction(world, kind):
    app = create_app(core_config(world, nodes={} if kind == "unconfigured" else core_config(world).nodes))
    auth = headers(world)
    if kind == "missing":
        auth = {}
    elif kind == "bad":
        import secrets
        auth = {"authorization": "Bearer " + secrets.token_urlsafe(32)}
    elif kind == "owner":
        auth = {"authorization": "Bearer " + world.owner_token.get_secret_value()}
    with TestClient(app, base_url=ORIGIN) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=auth) as ws:
                if kind == "wrong_type":
                    ws.send_text(wire_message("node.heartbeat", NodeHeartbeat(session_id=uuid4(), active_attempt_id=None)))
                elif kind == "fake_tool":
                    import json
                    raw = json.loads(wire_message("node.hello", hello(world)))
                    raw["payload"]["tools"][0]["tool"] = "shell.execute"
                    ws.send_text(json.dumps(raw))
                else:
                    ws.send_text(wire_message("node.hello", hello(world, node_id=uuid4() if kind == "forged_id" else world.node_id)))
                ws.receive_text()
        assert exc.value.code == 1008


def test_capability_intersection_and_dynamic_grant_revocation(world):
    grant(world)
    grant(world, "filesystem.list")
    app = create_app(core_config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/nodes").status_code == 401
        login(client, world)
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as ws:
            session = handshake(ws, world)
            status = client.get("/api/v1/nodes").json()["nodes"][0]
            assert status["online"] and status["session_id"] == str(session)
            assert status["tools"] == ["system.info@1"] and status["roots"] == []
            grant(world, enabled=False)
            assert client.get("/api/v1/nodes").json()["nodes"][0]["tools"] == []
    assert not app.state.node_link.connections


def test_advertisement_and_node_grant_cannot_replace_owner_grant(world):
    grant(world, owner=False)
    app = create_app(core_config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, world)
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as ws:
            handshake(ws, world)
            assert client.get("/api/v1/nodes").json()["nodes"][0]["tools"] == []


@pytest.mark.parametrize("kind", ["stale_session", "client_direction", "repeat_hello", "oversized", "binary"])
def test_running_connection_rejects_wrong_session_direction_and_size(world, kind):
    app = create_app(core_config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as ws:
            session = handshake(ws, world)
            if kind == "client_direction":
                raw = wire_message("command.status", CommandStatus(session_id=session, attempt_id=uuid4()))
            elif kind == "repeat_hello":
                raw = wire_message("node.hello", hello(world))
            else:
                raw = wire_message("node.heartbeat", NodeHeartbeat(session_id=uuid4() if kind == "stale_session" else session, active_attempt_id=None))
            if kind == "oversized":
                raw += " " * 65536
            if kind == "binary":
                ws.send_bytes(raw.encode())
            else:
                ws.send_text(raw)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 1008


def test_session_replacement_closes_old_connection_and_rejects_old_payload(world):
    app = create_app(core_config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as old:
            first = handshake(old, world)
            with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as new:
                second = handshake(new, world)
                assert first != second
                with pytest.raises(WebSocketDisconnect) as exc:
                    old.receive_text()
                assert exc.value.code == 1008
                new.send_text(wire_message("node.heartbeat", NodeHeartbeat(session_id=first, active_attempt_id=None)))
                with pytest.raises(WebSocketDisconnect) as exc:
                    new.receive_text()
                assert exc.value.code == 1008


def test_heartbeat_timeout_only_changes_presence(world):
    from nexus.contracts.task import SystemInfoArguments, SystemInfoRequest
    task = asyncio.run(world.tasks.create(world.owner_id, SystemInfoRequest(request_id=uuid4(), intent="get_system_info", arguments=SystemInfoArguments()), world.node_id))
    app = create_app(core_config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as ws:
            handshake(ws, world)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 1001
        assert asyncio.run(world.tasks.get(task.task_id, world.owner_id)) == task


@pytest.mark.parametrize("kind", ["revoke", "rotate"])
def test_idle_credential_change_closes_existing_connection(world, kind):
    app = create_app(core_config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as ws:
            handshake(ws, world)
            if kind == "revoke":
                asyncio.run(world.security.revoke_identity(world.node_id))
            else:
                import secrets
                from pydantic import SecretStr
                asyncio.run(world.security.rotate_credential(world.node_id, SecretStr(secrets.token_urlsafe(32))))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 1008
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)):
                pytest.fail("revoked credential reconnected")


def test_connection_audit_failure_prevents_welcome(world):
    async def inject():
        async with world.db.transaction() as conn:
            await conn.execute("CREATE TRIGGER failure BEFORE INSERT ON audit_events WHEN NEW.kind='node.connected' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    asyncio.run(inject())
    app = create_app(core_config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as ws:
            ws.send_text(wire_message("node.hello", hello(world)))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 1011
        assert not app.state.node_link.connections


def test_send_is_bounded_and_never_dispatches_execution(world):
    app = create_app(core_config(world))
    link = app.state.node_link
    with TestClient(app, base_url=ORIGIN) as client:
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as ws:
            session = handshake(ws, world)
            # Stop sender consumption for an actual bounded queue check.
            async def bounded():
                connection = link.connections[world.node_id]
                connection.outgoing = asyncio.Queue(1)
                payload = CommandStatus(session_id=session, attempt_id=uuid4())
                await link.send(world.node_id, "command.status", payload)
                with pytest.raises(ProtocolError) as exc:
                    await link.send(world.node_id, "command.status", payload)
                assert exc.value.error.code == ErrorCode.BUSY
                with pytest.raises(ProtocolError) as exc:
                    await link.send(world.node_id, "command.execute", payload)
                assert exc.value.error.code == ErrorCode.POLICY_DENIED
            client.portal.call(bounded)


def test_rate_window_bounded_and_refills(monkeypatch):
    import nexus.link as module
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    rate = MessageRate(maximum=2, window=10)
    rate.check()
    rate.check()
    with pytest.raises(ProtocolError):
        rate.check()
    assert len(rate.times) == 2
    clock[0] = 11
    rate.check()
    assert len(rate.times) == 1
