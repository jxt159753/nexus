"""Real owned Core/Windows Node processes; extension generation is a fixture."""

import json
import socket
import sqlite3
from contextlib import closing
from uuid import uuid4

import httpx2

from test_link_processes import spawn, stop, wait_for
from test_node_link import grant


def test_skill_real_devices_directory_approval_and_restart_without_replay(world, tmp_path):
    grant(world)
    grant(world, "filesystem.list")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    core_config = tmp_path / "core.toml"
    core_config.write_text(f'''data_dir={json.dumps(str(world.db.path.parent))}
owner_id="{world.owner_id}"
host="127.0.0.1"
port={port}
local_http=true
origins=["{origin}"]
heartbeat_interval_seconds=1
heartbeat_timeout_seconds=3
[nodes."{world.node_id}"]
tools=["system.info@1","filesystem.list@1"]
roots=["workspace"]
[extensions]
enabled=true
fixture_generation=true
''', encoding="utf-8")
    node_dir, workspace = tmp_path / "node", tmp_path / "workspace"
    node_dir.mkdir()
    workspace.mkdir()
    (workspace / "visible.txt").write_text("private content must not be returned", encoding="utf-8")
    (workspace / "docs").mkdir()
    node_config = tmp_path / "node.toml"
    node_config.write_text(f'''node_id="{world.node_id}"
data_dir={json.dumps(str(node_dir))}
core_url="ws://127.0.0.1:{port}/ws/v1/node"
local_http=true
allowed_tools=["system.info@1","filesystem.list@1"]
[roots]
workspace={json.dumps(str(workspace))}
''', encoding="utf-8")
    core = node = None
    with (tmp_path / "process.log").open("w+b") as log, httpx2.Client(base_url=origin, timeout=3, trust_env=False) as browser:
        try:
            core = spawn("nexus.core", core_config, log)
            wait_for(lambda: browser.get("/api/v1/tasks").status_code == 401)
            def login():
                response = browser.post("/api/v1/session", json={"token": world.owner_token.get_secret_value()}, headers={"origin": origin})
                assert response.status_code == 200
                return {"origin": origin, "x-csrf-token": response.json()["csrf_token"]}
            auth = login()
            def post(path, payload):
                response = browser.post("/api/v1/extensions" + path, json=payload, headers=auth)
                assert response.status_code == 200, response.text
                return response.json()
            node = spawn("nexus.node", node_config, log, token=world.node_token.get_secret_value())
            wait_for(lambda: browser.get("/api/v1/nodes").json()["nodes"][0]["online"])
            request = post("/requests", {"request_id": str(uuid4()), "kind": "skill", "need": "工作区概览", "node_id": str(world.node_id)})["request"]
            post("/requests/" + request["request_id"] + "/generate", {"preview_digest": request["preview_digest"], "allow_model": True})
            candidate = wait_for(lambda: (item if (item := browser.get("/api/v1/extensions/requests/" + request["request_id"]).json()["request"])["status"] == "candidate" else None))
            version_path = "/versions/" + candidate["candidate_version_id"]
            # Generation already schedules its bounded test. Wait for that
            # evidence instead of racing a duplicate manual test request.
            tested = wait_for(lambda: (item if (item := browser.get("/api/v1/extensions" + version_path).json()["version"])["test_status"] == "passed" else None))
            post(version_path + "/approve", {"decision": "approve", "artifact_digest": tested["artifact_digest"], "test_id": tested["test"]["test_id"]})
            post(version_path + "/enable", {})
            run = post("/runs", {"request_id": str(uuid4()), "version_id": tested["version_id"], "node_id": str(world.node_id), "input": {}})["run"]
            run_path = "/api/v1/extensions/runs/" + run["run_id"]
            def awaiting_directory():
                value = browser.get(run_path).json()["run"]
                return value if value["steps"][0]["status"] == "succeeded" and value["steps"][1]["status"] == "approval" else None
            waiting = wait_for(awaiting_directory)
            directory_id = waiting["steps"][1]["task_id"]
            detail_path = "/api/v1/tasks/" + directory_id
            assert browser.get(detail_path).json()["attempts"] == []
            old_pid = core.pid
            stop(core)
            core = spawn("nexus.core", core_config, log)
            wait_for(lambda: browser.get("/api/v1/session").status_code == 200)
            auth = login()
            wait_for(lambda: browser.get("/api/v1/nodes").json()["nodes"][0]["online"])
            restored = wait_for(awaiting_directory)
            assert [s["task_id"] for s in restored["steps"]] == [s["task_id"] for s in waiting["steps"]]
            approval = browser.get(detail_path).json()["approvals"][0]["approval_id"]
            assert browser.post("/api/v1/approvals/" + approval, json={"decision": "approve"}, headers=auth).status_code == 200
            done = wait_for(lambda: (value if (value := browser.get(run_path).json()["run"])["status"] == "succeeded" else None))
            details = [browser.get("/api/v1/tasks/" + s["task_id"]).json() for s in done["steps"]]
            assert core.pid != old_pid and node.pid != core.pid
            assert details[0]["task"]["result"]["os_name"] == "Windows"
            assert details[1]["task"]["result"]["entries"] == [{"name": "docs", "kind": "directory"}, {"name": "visible.txt", "kind": "file"}]
            assert all(d["task"]["verification"] == "node_report_schema_checked" and len(d["attempts"]) == 1 for d in details)
            with closing(sqlite3.connect(world.db.path)) as conn:
                assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
                assert conn.execute("SELECT count(*) FROM task_attempts").fetchone()[0] == 2
                assert conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
            (tmp_path / "evidence.json").write_text(json.dumps({"run": done, "tasks": details, "model_calls": 0}, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            stop(node)
            stop(core)
        log.seek(0)
        output = log.read().decode("utf-8", "replace")
        assert world.owner_token.get_secret_value() not in output
        assert world.node_token.get_secret_value() not in output
