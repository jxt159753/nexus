"""HTTP authentication and lifecycle endpoints for generated capabilities."""

import time
from uuid import uuid4

from fastapi.testclient import TestClient

from nexus.core.app import create_app
from nexus.core.config import ExtensionConfig, SandboxNodePolicy, TrustedNode
from test_harness_http import login
from test_node_link import ORIGIN, core_config


def configured(world, enabled=True):
    return create_app(core_config(
        world,
        extensions=ExtensionConfig(enabled=enabled, fixture_generation=enabled),
        nodes={world.node_id: TrustedNode(tools=frozenset({"system.info@1", "filesystem.list@1"}), roots=frozenset({"workspace"}))},
    ))


def test_extension_http_requires_owner_csrf_and_strict_fields(world):
    with TestClient(configured(world), base_url=ORIGIN) as client:
        assert client.get("/api/v1/extensions").status_code == 401
        auth = login(client, world)
        assert client.get("/api/v1/extensions").json()["extension"]["enabled"] is True
        body = {"request_id": str(uuid4()), "kind": "skill", "need": "工作区概览"}
        assert client.post("/api/v1/extensions/requests", json=body, headers={"origin": ORIGIN}).status_code == 403
        assert client.post("/api/v1/extensions/requests", json=body, headers={**auth, "origin": "https://evil.test"}).status_code == 403
        assert client.post("/api/v1/extensions/requests", json={**body, "extra": True}, headers=auth).status_code == 400
        created = client.post("/api/v1/extensions/requests", json=body, headers=auth)
        assert created.status_code == 200
        value = created.json()["request"]
        assert value["status"] == "draft" and value["outbound"]["mode"] == "fixture"
        request_id = value["request_id"]
        assert client.post(f"/api/v1/extensions/requests/{request_id}/generate", json={"preview_digest": "0" * 64, "allow_model": True}, headers=auth).status_code == 403
        queued = client.post(f"/api/v1/extensions/requests/{request_id}/generate", json={"preview_digest": value["preview_digest"], "allow_model": True}, headers=auth)
        assert queued.status_code == 200 and queued.json()["request"]["status"] in {"queued", "generating", "candidate"}
        for _ in range(60):
            current = client.get(f"/api/v1/extensions/requests/{request_id}", headers=auth).json()["request"]
            if current["status"] in {"candidate", "failed"}:
                break
            time.sleep(0.05)
        assert current["status"] == "candidate"
        versions = client.get("/api/v1/extensions/versions", headers=auth).json()["versions"]
        assert len(versions) == 1 and versions[0]["enabled"] is False
        version_id = versions[0]["version_id"]
        for _ in range(60):
            current_version = client.get(f"/api/v1/extensions/versions/{version_id}", headers=auth).json()["version"]
            if not current_version["test_running"] and current_version["test_status"] != "untested":
                break
            time.sleep(.05)
        assert client.post(f"/api/v1/extensions/versions/{version_id}/test", json={}, headers=auth).status_code == 200
        tested = client.get(f"/api/v1/extensions/versions/{version_id}", headers=auth).json()["version"]
        assert tested["test_status"] == "passed"
        assert client.post(f"/api/v1/extensions/versions/{version_id}/approve", json={
            "decision": "approve", "artifact_digest": tested["artifact_digest"], "test_id": tested["test"]["test_id"]}, headers=auth).status_code == 200
        assert client.post(f"/api/v1/extensions/versions/{version_id}/enable", json={}, headers=auth).status_code == 200
        enabled = client.get(f"/api/v1/extensions/versions/{version_id}", headers=auth).json()["version"]
        assert enabled["enabled"] is True
        assert client.post(f"/api/v1/extensions/versions/{version_id}/disable", json={"reason": "test"}, headers=auth).status_code == 200
        assert client.get(f"/api/v1/extensions/versions/{version_id}", headers=auth).json()["version"]["enabled"] is False


def test_disabled_extensions_preserve_existing_reads_but_refuse_new_work(world):
    with TestClient(configured(world), base_url=ORIGIN) as client:
        auth = login(client, world)
        body = {"request_id": str(uuid4()), "kind": "skill", "need": "工作区概览"}
        assert client.post("/api/v1/extensions/requests", json=body, headers=auth).status_code == 200
    with TestClient(configured(world, enabled=False), base_url=ORIGIN) as client:
        auth = login(client, world)
        assert client.get("/api/v1/extensions").json()["extension"]["enabled"] is False
        assert client.get("/api/v1/extensions/requests", headers=auth).status_code == 200
        refused = client.post("/api/v1/extensions/requests", json={"request_id": str(uuid4()), "kind": "skill", "need": "新的"}, headers=auth)
        assert refused.status_code == 403 and refused.json()["detail"] == "EXTENSIONS_DISABLED"


def test_registered_sandbox_status_endpoint_returns_offline_node(world):
    app = create_app(core_config(world, extensions=ExtensionConfig(enabled=True),
                                sandbox_nodes={world.node_id: SandboxNodePolicy(image_digest="a" * 64)}))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/extensions/sandbox").status_code == 401
        login(client, world)
        response = client.get("/api/v1/extensions/sandbox")
        assert response.status_code == 200
        assert response.json()["sandbox_nodes"] == [{
            "node_id": str(world.node_id), "runtime": "runsc", "image_digest": "a" * 64,
            "status": {"available": False, "reason": "sandbox_offline", "image_digest": "a" * 64}}]
