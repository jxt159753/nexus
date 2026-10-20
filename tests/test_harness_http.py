import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nexus.contracts.protocol import parse_message
from nexus.core.app import create_app
from nexus.core.config import TrustedNode
from nexus.link import wire_message
from test_harness import result_for
from test_node_link import ORIGIN, core_config, grant, handshake, headers


def login(client, world):
    response = client.post("/api/v1/session", json={"token": world.owner_token.get_secret_value()}, headers={"origin": ORIGIN})
    assert response.status_code == 200
    return {"origin": ORIGIN, "x-csrf-token": response.json()["csrf_token"]}


def submission(world, *, directory=False):
    return {"node_id": str(world.node_id), "request": {
        "request_id": str(uuid4()), "intent": "list_directory" if directory else "get_system_info",
        "arguments": {"root_id": "workspace", "limit": 2} if directory else {},
    }}


def test_http_session_csrf_strict_input_and_denied_action_history(world):
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        item = submission(world)
        assert client.get("/api/v1/tasks").status_code == 401
        assert client.post("/api/v1/tasks", json=item).status_code == 403
        auth = login(client, world)
        assert client.post("/api/v1/tasks", json=item, headers={"origin": ORIGIN}).status_code == 403
        assert client.post("/api/v1/tasks", json=item, headers={**auth, "origin": "https://evil.test"}).status_code == 403
        for bad in ({**item, "status": "succeeded"}, {**item, "request": {**item["request"], "arguments": {"path": "C:/"}}}):
            assert client.post("/api/v1/tasks", json=bad, headers=auth).status_code == 400
        unknown = {**item, "request": {**item["request"], "intent": "arbitrary_shell"}}
        assert client.post("/api/v1/tasks", json=unknown, headers=auth).json()["detail"] == "UNSUPPORTED_TASK"
        assert client.post("/api/v1/tasks", content=b" " * 32769, headers=auth).status_code == 413
        accepted = client.post("/api/v1/tasks", json=item, headers=auth)
        assert accepted.status_code == 200
        task = accepted.json()["task"]
        assert task["status"] == "failed" and task["error"]["code"] == "POLICY_DENIED"
        detail = client.get("/api/v1/tasks/" + task["task_id"])
        assert detail.headers["cache-control"] == "no-store"
        assert detail.json()["attempts"] == []
        assert [e["kind"] for e in detail.json()["events"]] == ["queued", "failed"]
        assert client.get("/api/v1/tasks/" + str(uuid4())).status_code == 404
        assert client.get("/api/v1/tasks?limit=101").status_code == 422


def test_http_idempotency_rate_limit_and_history_pages(world):
    grant(world)
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        auth = login(client, world)
        first = submission(world)
        task = client.post("/api/v1/tasks", json=first, headers=auth).json()["task"]
        assert task["status"] == "ready"  # offline never claims execution
        for _ in range(3):
            assert client.post("/api/v1/tasks", json=first, headers=auth).json()["task"]["task_id"] == task["task_id"]
        changed = submission(world, directory=True)
        changed["request"]["request_id"] = first["request"]["request_id"]
        assert client.post("/api/v1/tasks", json=changed, headers=auth).status_code == 409
        for _ in range(9):
            assert client.post("/api/v1/tasks", json=submission(world), headers=auth).status_code == 200
        assert client.post("/api/v1/tasks", json=submission(world), headers=auth).status_code == 429
        page = client.get("/api/v1/tasks?limit=3").json()
        second = client.get("/api/v1/tasks", params={"after": page["next_after"], "limit": 100}).json()
        assert len(page["tasks"]) == 3 and len(second["tasks"]) == 7
        assert not ({t["task_id"] for t in page["tasks"]} & {t["task_id"] for t in second["tasks"]})


def test_directory_http_approval_is_required_and_bound(world):
    grant(world, "filesystem.list")
    policy = TrustedNode(tools=frozenset({"filesystem.list@1"}), roots=frozenset({"workspace"}))
    with TestClient(create_app(core_config(world, nodes={world.node_id: policy})), base_url=ORIGIN) as client:
        auth = login(client, world)
        task = client.post("/api/v1/tasks", json=submission(world, directory=True), headers=auth).json()["task"]
        assert task["status"] == "awaiting_approval"
        detail = client.get("/api/v1/tasks/" + task["task_id"]).json()
        approval = detail["approvals"][0]["approval_id"]
        assert detail["attempts"] == []
        grant(world)  # policy version changes, stale approval must not unlock action
        response = client.post("/api/v1/approvals/" + approval, json={"decision": "approve"}, headers=auth)
        assert response.status_code == 403
        client.post("/api/v1/tasks", json={"node_id": str(world.node_id), "request": task["request"]}, headers=auth)
        updated = client.get("/api/v1/tasks/" + task["task_id"]).json()
        new = updated["approvals"][0]["approval_id"]
        assert new != approval
        response = client.post("/api/v1/approvals/" + new, json={"decision": "reject"}, headers=auth)
        assert response.json()["task"]["status"] == "failed"
        assert client.get("/api/v1/tasks/" + task["task_id"]).json()["attempts"] == []


@pytest.mark.parametrize("mismatch", [False, True])
def test_real_core_ws_persists_before_dispatch_and_acknowledges_only_valid_result(world, mismatch):
    grant(world)
    app = create_app(core_config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        auth = login(client, world)
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as ws:
            session = handshake(ws, world)
            task = client.post("/api/v1/tasks", json=submission(world), headers=auth).json()["task"]
            command = parse_message(ws.receive_text())
            assert command.type == "command.execute" and command.payload.session_id == session
            detail = client.get("/api/v1/tasks/" + task["task_id"]).json()
            assert detail["task"]["status"] == "dispatching" and len(detail["attempts"]) == 1
            result = result_for(command.payload)
            if mismatch:
                result = result.model_copy(update={"command_digest": "f" * 64})
            ws.send_text(wire_message("command.result", result))
            if mismatch:
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_text()
                assert closed.value.code == 1008
                current = client.get("/api/v1/tasks/" + task["task_id"]).json()
                assert current["task"]["status"] == "dispatching"
                assert current["task"]["result"] is None
            else:
                ack = parse_message(ws.receive_text())
                assert ack.type == "result.ack" and ack.payload.status == "stored"
                current = client.get("/api/v1/tasks/" + task["task_id"]).json()
                assert current["task"]["status"] == "succeeded"
                ws.send_text(wire_message("command.result", result))
                assert parse_message(ws.receive_text()).payload.status == "already_settled"
                assert client.get("/api/v1/tasks/" + task["task_id"]).json() == current


def test_dispatch_storage_failure_closes_link_and_stops_new_submissions(world):
    grant(world)
    app = create_app(core_config(world))
    with TestClient(app, base_url=ORIGIN) as client:
        auth = login(client, world)
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as ws:
            handshake(ws, world)
            async def fail():
                async with world.db.transaction() as conn:
                    await conn.execute("CREATE TRIGGER injected BEFORE INSERT ON task_attempts BEGIN SELECT RAISE(ABORT, 'injected'); END")
            asyncio.run(fail())
            response = client.post("/api/v1/tasks", json=submission(world), headers=auth)
            assert response.status_code == 200  # acceptance is already durable
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
            assert app.state.harness.failed
            assert client.post("/api/v1/tasks", json=submission(world), headers=auth).status_code == 503
            detail = client.get("/api/v1/tasks/" + response.json()["task"]["task_id"]).json()
            assert detail["task"]["status"] == "ready" and detail["attempts"] == []


def test_revoked_pending_action_does_not_starve_another_allowed_tool(world):
    grant(world)
    grant(world, "filesystem.list")
    policy = TrustedNode(tools=frozenset({"system.info@1", "filesystem.list@1"}), roots=frozenset({"workspace"}))
    with TestClient(create_app(core_config(world, nodes={world.node_id: policy})), base_url=ORIGIN) as client:
        auth = login(client, world)
        directory = client.post("/api/v1/tasks", json=submission(world, directory=True), headers=auth).json()["task"]
        detail = client.get("/api/v1/tasks/" + directory["task_id"]).json()
        approval_id = detail["approvals"][0]["approval_id"]
        assert client.post("/api/v1/approvals/" + approval_id, json={"decision": "approve"}, headers=auth).status_code == 200
        grant(world, "filesystem.list", enabled=False)
        system = client.post("/api/v1/tasks", json=submission(world), headers=auth).json()["task"]
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as ws:
            handshake(ws, world)
            command = parse_message(ws.receive_text())
            assert command.type == "command.execute" and str(command.payload.task_id) == system["task_id"]
            assert client.get("/api/v1/tasks/" + directory["task_id"]).json()["attempts"] == []
