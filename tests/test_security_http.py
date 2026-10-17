import asyncio
import json
import secrets

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nexus.core.security.http import COOKIE, SecurityHTTP, TransportPolicy
from nexus.core.storage.database import timestamp, utc_now

ORIGIN = "https://nexus.test"


@pytest.fixture
def application(world):
    guards = SecurityHTTP(world.security, TransportPolicy(frozenset({ORIGIN})))
    app = FastAPI()
    app.include_router(guards.router)

    @app.api_route("/protected", methods=["GET", "POST"])
    async def protected(request: Request):
        principal = await guards.owner_http(request)
        return {"role": principal.role}

    @app.websocket("/browser")
    async def browser(ws: WebSocket):
        principal = await guards.browser_ws(ws)
        await ws.accept()
        await guards.receive_text(ws, principal)
        await ws.send_json({"role": principal.role})

    @app.websocket("/node")
    async def node(ws: WebSocket):
        principal = await guards.node_ws(ws)
        await ws.accept()
        await guards.receive_text(ws, principal)
        await ws.send_json({"role": principal.role, "node_id": str(principal.identity_id)})

    return app


def login(client, world):
    return client.post("/api/v1/session", json={"token": world.owner_token.get_secret_value()}, headers={"origin": ORIGIN})


def test_cookie_attributes_http_auth_origin_csrf_and_logout(world, application):
    with TestClient(application, base_url=ORIGIN) as client:
        assert client.get("/protected").status_code == 401
        response = login(client, world)
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert all(part in cookie for part in ("httponly", "secure", "samesite=strict", "max-age=28800"))
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/protected").json() == {"role": "owner"}
        csrf = response.json()["csrf_token"]
        assert client.post("/protected", headers={"origin": ORIGIN}).status_code == 403
        assert client.post("/protected", headers={"origin": "https://evil.test", "x-csrf-token": csrf}).status_code == 403
        assert client.post("/protected", headers={"origin": ORIGIN, "x-csrf-token": secrets.token_urlsafe(32)}).status_code == 403
        headers = {"origin": ORIGIN, "x-csrf-token": csrf}
        assert client.post("/protected", headers=headers).status_code == 200
        assert client.delete("/api/v1/session", headers=headers).status_code == 200
        assert client.get("/protected").status_code == 401


@pytest.mark.parametrize("kind", ["no_origin", "evil_origin", "node_token", "bad_token", "extra", "huge", "invalid_json"])
def test_login_rejects_bad_or_oversized_input_without_echoing_token(world, application, kind):
    with TestClient(application, base_url=ORIGIN) as client:
        payload = {"token": world.owner_token.get_secret_value()}
        headers = {"origin": ORIGIN}
        if kind == "no_origin":
            headers = {}
        elif kind == "evil_origin":
            headers["origin"] = "https://evil.test"
        elif kind == "node_token":
            payload["token"] = world.node_token.get_secret_value()
        elif kind == "bad_token":
            payload["token"] = secrets.token_urlsafe(32)
        elif kind == "extra":
            payload["owner"] = True
        content = "{" if kind == "invalid_json" else json.dumps(payload) + (" " * 4096 if kind == "huge" else "")
        response = client.post("/api/v1/session", content=content, headers=headers)
        assert response.status_code in {400, 401, 403, 413}
        assert "set-cookie" not in response.headers
        assert world.owner_token.get_secret_value() not in response.text
        assert world.node_token.get_secret_value() not in response.text


@pytest.mark.parametrize("endpoint,kind", [("browser", "missing"), ("browser", "evil_origin"), ("browser", "node_cookie"), ("node", "missing"), ("node", "owner_token"), ("node", "session_token"), ("node", "bad_token")])
def test_ws_handshake_without_correct_identity_is_rejected(world, application, endpoint, kind):
    with TestClient(application, base_url=ORIGIN) as client:
        headers = {"origin": ORIGIN}
        if kind == "evil_origin":
            login(client, world)
            headers["origin"] = "https://evil.test"
        elif kind == "node_cookie":
            headers["cookie"] = f"{COOKIE}={world.node_token.get_secret_value()}"
        elif kind == "owner_token":
            headers["authorization"] = "Bearer " + world.owner_token.get_secret_value()
        elif kind == "session_token":
            login(client, world)
            headers["authorization"] = "Bearer " + client.cookies[COOKIE]
        elif kind == "bad_token":
            headers["authorization"] = "Bearer " + secrets.token_urlsafe(32)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"wss://nexus.test/{endpoint}", headers=headers):
                pytest.fail("unauthorized connection accepted")
        assert exc.value.code == 1008


def test_authenticated_browser_and_node_handshakes(world, application):
    with TestClient(application, base_url=ORIGIN) as client:
        assert login(client, world).status_code == 200
        with client.websocket_connect("wss://nexus.test/browser", headers={"origin": ORIGIN}) as ws:
            ws.send_text("check")
            assert ws.receive_json() == {"role": "owner"}
        with client.websocket_connect("wss://nexus.test/node", headers={"authorization": "Bearer " + world.node_token.get_secret_value()}) as ws:
            ws.send_text("check")
            assert ws.receive_json() == {"role": "node", "node_id": str(world.node_id)}


@pytest.mark.parametrize("kind", ["node_revoke", "owner_revoke", "session_expire", "idle_revoke"])
def test_existing_ws_cannot_send_after_revocation_or_expiry(world, application, kind):
    with TestClient(application, base_url=ORIGIN) as client:
        if kind in {"node_revoke", "idle_revoke"}:
            url = "wss://nexus.test/node"
            headers = {"authorization": "Bearer " + world.node_token.get_secret_value()}
        else:
            assert login(client, world).status_code == 200
            url, headers = "wss://nexus.test/browser", {"origin": ORIGIN}
        with client.websocket_connect(url, headers=headers) as ws:
            if kind == "session_expire":
                async def expire():
                    async with world.db.transaction() as conn:
                        await conn.execute("UPDATE sessions SET expires_at=?", (timestamp(utc_now()),))
                asyncio.run(expire())
            else:
                asyncio.run(world.security.revoke_identity(world.node_id if kind in {"node_revoke", "idle_revoke"} else world.owner_id))
            if kind != "idle_revoke":
                ws.send_text("should not execute")
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1008


@pytest.mark.parametrize("origin,local", [("http://0.0.0.0:8000", True), ("http://192.168.1.2:8000", True), ("http://localhost:8000", False), ("https://nexus.test/path", False), ("https://user@nexus.test", False), ("null", False), ("*", False)])
def test_transport_configuration_rejects_insecure_or_nonexact_origins(origin, local):
    with pytest.raises(ValueError):
        TransportPolicy(frozenset({origin}), local_http=local)


def test_secure_mode_rejects_plain_http_and_local_mode_is_explicit(world, application):
    with TestClient(application, base_url="http://nexus.test") as client:
        assert login(client, world).status_code == 403
    local_origin = "http://127.0.0.1:8000"
    local = SecurityHTTP(world.security, TransportPolicy(frozenset({local_origin}), local_http=True))
    app = FastAPI()
    app.include_router(local.router)
    with TestClient(app, base_url=local_origin) as client:
        response = client.post("/api/v1/session", json={"token": world.owner_token.get_secret_value()}, headers={"origin": local_origin})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "; secure" not in cookie
