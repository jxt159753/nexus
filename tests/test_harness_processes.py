"""Actual Windows tools through two CLI processes and a real HTTP/WS socket."""

import json
import socket
import sqlite3
from contextlib import closing

import httpx2

from test_harness_http import submission
from test_link_processes import spawn, stop, wait_for
from test_node_link import grant


def test_independent_process_read_only_loop_approval_and_durable_history(world, tmp_path):
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
''', encoding="utf-8")
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "read-me.txt").write_text("must not return file content", encoding="utf-8")
    (root / "目录").mkdir()
    node_config = tmp_path / "node.toml"
    node_config.write_text(f'''node_id="{world.node_id}"
data_dir={json.dumps(str(node_dir))}
core_url="ws://127.0.0.1:{port}/ws/v1/node"
local_http=true
allowed_tools=["system.info@1","filesystem.list@1"]
[roots]
workspace={json.dumps(str(root))}
''', encoding="utf-8")
    core, node = None, None
    log_path = tmp_path / "process.log"
    with log_path.open("w+b") as log, httpx2.Client(base_url=origin, timeout=3, trust_env=False) as browser:
        try:
            core = spawn("nexus.core", core_config, log)
            wait_for(lambda: browser.get("/api/v1/tasks").status_code == 401)
            def login():
                response = browser.post("/api/v1/session", json={"token": world.owner_token.get_secret_value()}, headers={"origin": origin})
                assert response.status_code == 200
                return {"origin": origin, "x-csrf-token": response.json()["csrf_token"]}
            auth = login()
            node = spawn("nexus.node", node_config, log, token=world.node_token.get_secret_value())
            wait_for(lambda: browser.get("/api/v1/nodes").json()["nodes"][0]["online"])
            assert core.pid != node.pid
            system_request = submission(world)
            accepted = browser.post("/api/v1/tasks", json=system_request, headers=auth)
            assert accepted.status_code == 200
            system_id = accepted.json()["task"]["task_id"]
            def succeeded(task_id):
                detail = browser.get("/api/v1/tasks/" + task_id).json()
                return detail if detail["task"]["status"] == "succeeded" else None
            system = wait_for(lambda: succeeded(system_id))
            assert set(system["task"]["result"]) == {"os_name", "os_version", "architecture", "python_version"}
            assert system["task"]["result"]["os_name"] == "Windows"
            assert system["task"]["verification"] == "node_report_schema_checked"
            again = browser.post("/api/v1/tasks", json=system_request, headers=auth)
            assert again.json()["task"]["task_id"] == system_id
            directory_request = submission(world, directory=True)
            directory = browser.post("/api/v1/tasks", json=directory_request, headers=auth).json()["task"]
            assert directory["status"] == "awaiting_approval"
            detail = browser.get("/api/v1/tasks/" + directory["task_id"]).json()
            assert detail["attempts"] == []
            approval = detail["approvals"][0]["approval_id"]
            decision = browser.post("/api/v1/approvals/" + approval, json={"decision": "approve"}, headers=auth)
            assert decision.status_code == 200
            listing = wait_for(lambda: succeeded(directory["task_id"]))
            assert listing["task"]["result"] == {"root_id": "workspace", "entries": [
                {"name": "read-me.txt", "kind": "file"}, {"name": "目录", "kind": "directory"}], "truncated": False}
            snapshots = [system, listing]
            def acknowledged():
                with closing(sqlite3.connect(node_dir / "receipts.sqlite3")) as conn:
                    return conn.execute("SELECT count(*) FROM execution_receipts WHERE acknowledged=1").fetchone()[0] == 2
            wait_for(acknowledged)
            with closing(sqlite3.connect(node_dir / "receipts.sqlite3")) as conn:
                assert conn.execute("SELECT count(*) FROM execution_receipts").fetchone()[0] == 2
            old_pid = core.pid
            stop(core)
            core = spawn("nexus.core", core_config, log)
            wait_for(lambda: browser.get("/api/v1/tasks").status_code == 200)
            auth = login()
            wait_for(lambda: browser.get("/api/v1/nodes").json()["nodes"][0]["online"])
            assert core.pid != old_pid
            for snapshot in snapshots:
                restored = browser.get("/api/v1/tasks/" + snapshot["task"]["task_id"]).json()
                assert restored == snapshot
                assert len(restored["attempts"]) == 1
            with closing(sqlite3.connect(world.db.path)) as conn:
                assert conn.execute("SELECT count(*) FROM task_attempts").fetchone()[0] == 2
                assert conn.execute("SELECT count(*) FROM execution_grants").fetchone()[0] == 2
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                assert "model_calls" in tables
                assert conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
            (tmp_path / "evidence.json").write_text(json.dumps({
                "old_core_pid": old_pid, "restarted_core_pid": core.pid, "node_pid": node.pid,
                "system_task": system_id, "directory_task": directory["task_id"],
                "snapshots": snapshots, "model_calls": 0,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            stop(node)
            stop(core)
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace")
        assert world.owner_token.get_secret_value() not in output
        assert world.node_token.get_secret_value() not in output
