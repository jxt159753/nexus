"""Real browser-protocol socket, independent Core/Windows Node and restart."""

import json
import socket
import sqlite3
from contextlib import closing
from uuid import uuid4

import httpx2
from websockets.sync.client import connect

from nexus.contracts.protocol import parse_message
from test_link_processes import spawn, stop, wait_for
from test_node_link import grant


def receive(ws, kind, predicate=lambda payload: True):
    for _ in range(100):
        message = parse_message(ws.recv(timeout=5))
        assert message.type != "error", message
        if message.type == kind and predicate(message.payload): return message.payload
    raise AssertionError("bounded browser event replay exhausted")


def send(ws, kind, payload):
    from datetime import UTC, datetime
    ws.send(json.dumps({"protocol_version":1,"type":kind,"message_id":str(uuid4()),"correlation_id":None,"sent_at":datetime.now(UTC).isoformat(),"payload":payload}))


def test_browser_ws_real_windows_tools_records_and_core_restart(world, tmp_path):
    grant(world); grant(world, "filesystem.list")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1",0)); port = reserved.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    core_config = tmp_path / "core.toml"
    core_config.write_text(f'''data_dir={json.dumps(str(world.db.path.parent))}
owner_id="{world.owner_id}"
host="127.0.0.1"
port={port}
local_http=true
origins=["{origin}"]
[nodes."{world.node_id}"]
tools=["system.info@1","filesystem.list@1"]
roots=["workspace"]
''', encoding="utf-8")
    node_dir, root = tmp_path / "node", tmp_path / "workspace"
    node_dir.mkdir(); root.mkdir()
    (root / "safe & text.txt").write_text("must not return content", encoding="utf-8")
    node_config = tmp_path / "node.toml"
    node_config.write_text(f'''node_id="{world.node_id}"
data_dir={json.dumps(str(node_dir))}
core_url="ws://127.0.0.1:{port}/ws/v1/node"
local_http=true
allowed_tools=["system.info@1","filesystem.list@1"]
[roots]
workspace={json.dumps(str(root))}
''', encoding="utf-8")
    core = node = None
    with (tmp_path / "process.log").open("w+b") as log, httpx2.Client(base_url=origin, timeout=3, trust_env=False) as browser:
        try:
            core = spawn("nexus.core", core_config, log)
            wait_for(lambda: browser.get("/").status_code == 200)
            auth_response = browser.post("/api/v1/session", json={"token":world.owner_token.get_secret_value()}, headers={"origin":origin})
            assert auth_response.status_code == 200
            auth = {"origin":origin,"x-csrf-token":auth_response.json()["csrf_token"]}
            project = browser.post("/api/v1/projects", json={"name":"WS 演示"}, headers=auth).json()["project"]
            memory = browser.post("/api/v1/memories", json={"request_id":str(uuid4()),"consent":True,"content":"测试记忆","kind":"plan","source":"独立进程验证","project_id":project["project_id"]}, headers=auth).json()["memory"]
            node = spawn("nexus.node", node_config, log, token=world.node_token.get_secret_value())
            wait_for(lambda: browser.get("/api/v1/nodes").json()["nodes"][0]["online"])
            cookie = "nexus_session=" + browser.cookies.get("nexus_session")
            ws_url = f"ws://127.0.0.1:{port}/ws/v1/client"
            with connect(ws_url, origin=origin, additional_headers={"Cookie":cookie}, proxy=None) as ws:
                send(ws,"client.sync",{"event_id":None})
                assert receive(ws,"client.synced").full_sync_required
                request = {"request_id":str(uuid4()),"intent":"get_system_info","arguments":{},"project_id":project["project_id"]}
                send(ws,"task.submit",request)
                accepted = receive(ws,"task.accepted")
                result = receive(ws,"task.updated",lambda p:p.status == "succeeded")
                assert result.task_id == accepted.task_id and result.result.os_name == "Windows"
                assert result.verification == "node_report_schema_checked"
                send(ws,"task.submit",request)
                assert receive(ws,"task.accepted").task_id == result.task_id
                send(ws,"task.submit",{"request_id":str(uuid4()),"intent":"list_directory","arguments":{"root_id":"workspace","limit":1}})
                receive(ws,"task.accepted")
                approval = receive(ws,"approval.required")
                send(ws,"approval.decide",{"approval_id":str(approval.approval_id),"decision":"approve"})
                directory = receive(ws,"task.updated",lambda p:p.status == "succeeded")
                assert directory.result.entries[0].name == "safe & text.txt"
                cursor = directory.event_id
                send(ws,"control.pause",{"request_id":str(uuid4())})
                paused = receive(ws,"control.updated")
                assert paused.paused and int(paused.event_id) > int(cursor)
            old_pid = core.pid
            stop(core); core = spawn("nexus.core",core_config,log)
            wait_for(lambda: browser.get("/api/v1/session").status_code == 200)
            assert core.pid != old_pid and node.pid != core.pid
            assert browser.get("/api/v1/memories").json()["memories"] == [memory]
            assert len(browser.get("/api/v1/records/routes").json()["routes"]) == 2
            assert browser.get("/api/v1/models/usage").json()["calls"] == 0
            with connect(ws_url,origin=origin,additional_headers={"Cookie":cookie},proxy=None) as ws:
                send(ws,"client.sync",{"event_id":cursor})
                assert not receive(ws,"client.synced").full_sync_required
                assert receive(ws,"control.updated").paused
                send(ws,"task.submit",{"request_id":str(uuid4()),"intent":"get_system_info","arguments":{}})
                queued = receive(ws,"task.accepted")
                send(ws,"control.cancel_all",{"request_id":str(uuid4())})
                assert receive(ws,"task.updated",lambda p:p.status=="cancelled").task_id == queued.task_id
                send(ws,"control.resume",{"request_id":str(uuid4())})
                assert not receive(ws,"control.updated",lambda p:not p.paused).paused
            with closing(sqlite3.connect(world.db.path)) as conn:
                assert conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
                assert conn.execute("SELECT count(*) FROM routing_decisions").fetchone()[0] == 3
                assert conn.execute("SELECT count(*) FROM task_attempts").fetchone()[0] == 2
            (tmp_path / "evidence.json").write_text(json.dumps({"old_core_pid":old_pid,"new_core_pid":core.pid,"node_pid":node.pid,"system_task":str(result.task_id),"directory_task":str(directory.task_id),"cursor":cursor,"verified":result.verification,"model_calls":0,"memory_consent":memory["consent_event_id"]},indent=2), encoding="utf-8")
        finally:
            stop(node); stop(core)
        log.seek(0); output = log.read().decode("utf-8", errors="replace")
        assert world.owner_token.get_secret_value() not in output and world.node_token.get_secret_value() not in output
