"""S7: real TLS sockets and offline recovery into fresh data directories.

OpenSSL is a test-only executable. Certificates and keys are generated under
tmp_path; trust is limited to explicit SSL contexts, never the OS trust store.
"""

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import ssl
import subprocess
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from nexus.contracts.protocol import parse_message

from test_browser_processes import receive, send
from test_link_processes import spawn, stop, wait_for
from test_node_link import grant


@pytest.fixture
def tls_files(tmp_path):
    executable = os.environ.get("NEXUS_TEST_OPENSSL") or shutil.which("openssl")
    if not executable:
        pytest.skip("S7 TLS integration requires a test-only OpenSSL executable")

    def run(*args):
        subprocess.run([executable, *args], cwd=tmp_path, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)

    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-keyout", "ca.key", "-out", "ca.pem", "-subj", "/CN=NEXUS S7 test CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key",
        "-out", "server.csr", "-subj", "/CN=localhost")
    (tmp_path / "server.ext").write_text(
        "subjectAltName=DNS:localhost\nbasicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n", encoding="ascii")
    run("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
        "-CAcreateserial", "-out", "server.pem", "-days", "2", "-extfile", "server.ext")
    return tmp_path


def tls_context(directory):
    context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    return context


def configuration(world, tmp_path, tls):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    origin = f"https://localhost:{port}"
    root, node_dir = tmp_path / "workspace", tmp_path / "node"
    root.mkdir(); node_dir.mkdir()
    (root / "safe & text.txt").write_text("file contents must stay private", encoding="utf-8")
    (root / "目录").mkdir()

    def write(core_dir, receipts_dir, suffix):
        core_path, node_path = tmp_path / f"core{suffix}.toml", tmp_path / f"node{suffix}.toml"
        core_path.write_text(f'''data_dir={json.dumps(str(core_dir))}
owner_id="{world.owner_id}"
host="127.0.0.1"
port={port}
local_http=false
origins=["{origin}"]
tls_cert={json.dumps(str(tls / "server.pem"))}
tls_key={json.dumps(str(tls / "server.key"))}
[nodes."{world.node_id}"]
tools=["system.info@1","filesystem.list@1"]
roots=["workspace"]
''', encoding="utf-8")
        node_path.write_text(f'''node_id="{world.node_id}"
data_dir={json.dumps(str(receipts_dir))}
core_url="wss://localhost:{port}/ws/v1/node"
local_http=false
ca_file={json.dumps(str(tls / "ca.pem"))}
allowed_tools=["system.info@1","filesystem.list@1"]
[roots]
workspace={json.dumps(str(root))}
''', encoding="utf-8")
        return core_path, node_path

    return origin, port, node_dir, write


def login(browser, origin, world):
    response = browser.post("/api/v1/session", headers={"origin": origin},
                            json={"token": world.owner_token.get_secret_value()})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert all(value in cookie for value in ("secure", "httponly", "samesite=strict"))
    return {"origin": origin, "x-csrf-token": response.json()["csrf_token"]}


def backup(source: Path, target: Path):
    # Both owned processes are stopped. mode=rw refuses a missing source and
    # lets SQLite recover a WAL left by Windows TerminateProcess before backup.
    with target.open("xb"):
        pass
    with closing(sqlite3.connect(source.as_uri() + "?mode=rw", uri=True)) as src:
        with closing(sqlite3.connect(target)) as dest:
            src.backup(dest)
            assert dest.execute("PRAGMA quick_check").fetchall() == [("ok",)]
            assert dest.execute("PRAGMA foreign_key_check").fetchall() == []
    return hashlib.sha256(target.read_bytes()).hexdigest()


def test_https_wss_real_tools_and_certificate_rejection(world, tmp_path, tls_files):
    grant(world); grant(world, "filesystem.list")
    origin, port, node_dir, write = configuration(world, tmp_path, tls_files)
    core_path, node_path = write(world.db.path.parent, node_dir, "")
    core = node = None
    with (tmp_path / "process.log").open("w+b") as log, httpx2.Client(
            base_url=origin, verify=tls_context(tls_files), trust_env=False, timeout=3) as browser:
        try:
            core = spawn("nexus.core", core_path, log)
            wait_for(lambda: browser.get("/").status_code == 200)
            assert browser.get("/static/app.js").status_code == 200
            # Real TLS handshakes must reject both an untrusted CA and a wrong host.
            for context, hostname in ((ssl.create_default_context(), "localhost"),
                                      (tls_context(tls_files), "127.0.0.1")):
                with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
                    with pytest.raises(ssl.SSLCertVerificationError):
                        context.wrap_socket(raw, server_hostname=hostname)
            auth = login(browser, origin, world)
            node = spawn("nexus.node", node_path, log, token=world.node_token.get_secret_value())
            wait_for(lambda: browser.get("/api/v1/nodes").json()["nodes"][0]["online"])
            assert core.pid != node.pid
            ws_url = origin.replace("https:", "wss:") + "/ws/v1/client"
            cookie = "nexus_session=" + browser.cookies.get("nexus_session")
            for headers, ws_origin in (({}, origin), ({"Cookie": cookie}, "https://untrusted.invalid")):
                with pytest.raises(InvalidStatus) as denied:
                    with connect(ws_url, ssl=tls_context(tls_files), origin=ws_origin,
                                 additional_headers=headers, proxy=None):
                        pytest.fail("unauthorized WSS connection accepted")
                assert denied.value.response.status_code == 403
            with connect(ws_url, ssl=tls_context(tls_files), origin=origin,
                         additional_headers={"Cookie": cookie}, proxy=None) as ws:
                send(ws, "client.sync", {"event_id": None})
                receive(ws, "client.synced")
                request = {"request_id": str(uuid4()), "intent": "get_system_info", "arguments": {}}
                send(ws, "task.submit", request)
                accepted = receive(ws, "task.accepted")
                system = receive(ws, "task.updated", lambda p: p.status == "succeeded")
                assert accepted.task_id == system.task_id and system.result.os_name == "Windows"
                assert system.verification == "node_report_schema_checked"
                send(ws, "task.submit", request)
                assert receive(ws, "task.accepted").task_id == system.task_id
                conflict = browser.post("/api/v1/tasks", headers=auth, json={
                    "node_id": str(world.node_id), "request": dict(request,
                    intent="list_directory", arguments={"root_id": "workspace", "limit": 1})})
                assert conflict.status_code == 409
                send(ws, "task.submit", {"request_id": str(uuid4()), "intent": "list_directory",
                                         "arguments": {"root_id": "workspace", "limit": 1}})
                # The scheduler can publish approval.required before the submit
                # acknowledgement. Do not discard it while waiting for accepted.
                accepted = approval = None
                deadline = time.monotonic()+5
                for _ in range(100):
                    message = parse_message(ws.recv(timeout=max(.01,deadline-time.monotonic())))
                    assert message.type != "error", message
                    if message.type == "task.accepted":
                        accepted = message.payload
                    elif message.type == "approval.required":
                        approval = message.payload
                    if accepted is not None and approval is not None:
                        break
                    assert time.monotonic()<deadline
                assert accepted is not None and approval is not None and accepted.task_id==approval.task_id
                pending = browser.get(f"/api/v1/tasks/{approval.task_id}").json()
                assert pending["task"]["status"] == "awaiting_approval" and not pending["attempts"]
                send(ws, "approval.decide", {"approval_id": str(approval.approval_id), "decision": "approve"})
                directory = receive(ws, "task.updated", lambda p: p.status == "succeeded")
                assert directory.result.truncated and directory.result.entries[0].name == "safe & text.txt"
                assert browser.get("/api/v1/models/usage").json()["calls"] == 0
            (tmp_path / "evidence.json").write_text(json.dumps({
                "transport": "HTTPS/WSS with explicit isolated CA and hostname verification",
                "core_pid": core.pid, "node_pid": node.pid, "system_task": str(system.task_id),
                "directory_task": str(directory.task_id), "verification": system.verification,
                "certificate_rejections": ["untrusted CA", "hostname mismatch"],
                "wss_auth_rejections": ["missing cookie", "wrong origin"],
                "idempotency_conflict_status": 409, "model_calls": 0,
            }, indent=2), encoding="utf-8")
        finally:
            stop(node); stop(core)
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace")
        assert all(token.get_secret_value() not in output for token in (world.owner_token, world.node_token))


def test_offline_backup_restore_history_scope_approval_and_node_reconnect(world, tmp_path, tls_files):
    grant(world); grant(world, "filesystem.list")
    origin, _, node_dir, write = configuration(world, tmp_path, tls_files)
    core_path, node_path = write(world.db.path.parent, node_dir, "")
    core = node = None
    with (tmp_path / "process.log").open("w+b") as log, httpx2.Client(
            base_url=origin, verify=tls_context(tls_files), trust_env=False, timeout=3) as browser:
        try:
            core = spawn("nexus.core", core_path, log)
            wait_for(lambda: browser.get("/").status_code == 200)
            auth = login(browser, origin, world)
            node = spawn("nexus.node", node_path, log, token=world.node_token.get_secret_value())
            before_node = wait_for(lambda: next((n for n in browser.get("/api/v1/nodes").json()["nodes"] if n["online"]), None))
            project = browser.post("/api/v1/projects", headers=auth, json={"name": "S7 synthetic recovery"}).json()["project"]
            memory = browser.post("/api/v1/memories", headers=auth, json={"request_id": str(uuid4()),
                "consent": True, "kind": "plan", "content": "Synthetic restore exercise",
                "source": "S7 test", "project_id": project["project_id"]}).json()["memory"]
            def submit(intent, arguments):
                response = browser.post("/api/v1/tasks", headers=auth, json={"node_id": str(world.node_id),
                    "request": {"request_id": str(uuid4()), "intent": intent, "arguments": arguments,
                                "project_id": project["project_id"]}})
                assert response.status_code == 200
                return response.json()["task"]
            task = submit("get_system_info", {})
            def completed():
                detail = browser.get("/api/v1/tasks/" + task["task_id"]).json()
                return detail if detail["task"]["status"] == "succeeded" else None
            snapshot = wait_for(completed)
            def acknowledged():
                with closing(sqlite3.connect(node_dir / "receipts.sqlite3")) as conn:
                    return conn.execute("SELECT acknowledged FROM execution_receipts").fetchone() == (1,)
            wait_for(acknowledged)
            pending = submit("list_directory", {"root_id": "workspace", "limit": 10})
            assert pending["status"] == "awaiting_approval"
            pending_snapshot = browser.get("/api/v1/tasks/" + pending["task_id"]).json()
            paused = browser.post("/api/v1/control/pause", headers=auth, json={"request_id": str(uuid4())}).json()
            assert paused["paused"]
            paused = browser.get("/api/v1/control").json()
            original_pids = [core.pid, node.pid]
            stop(node); stop(core)
            assert all(process.poll() is not None for process in (node, core))
            core = node = None
            archive = tmp_path / "backup"
            archive.mkdir()
            checksums = {"core": backup(world.db.path, archive / "core.sqlite3"),
                         "node": backup(node_dir / "receipts.sqlite3", archive / "receipts.sqlite3")}
            restored_core, restored_node = tmp_path / "restored-core", tmp_path / "restored-node"
            restored_core.mkdir(); restored_node.mkdir()
            for source, target in ((archive / "core.sqlite3", restored_core / "core.sqlite3"),
                                   (archive / "receipts.sqlite3", restored_node / "receipts.sqlite3")):
                with source.open("rb") as src, target.open("xb") as dest:
                    shutil.copyfileobj(src, dest)
                assert source.read_bytes() == target.read_bytes()
            restored_config, restored_node_config = write(restored_core, restored_node, "-restored")
            browser.cookies.clear()
            core = spawn("nexus.core", restored_config, log)
            wait_for(lambda: browser.get("/").status_code == 200)
            auth = login(browser, origin, world)
            offline = browser.get("/api/v1/nodes").json()["nodes"][0]
            assert not offline["online"]
            assert browser.get("/api/v1/tasks/" + task["task_id"]).json() == snapshot
            assert browser.get("/api/v1/tasks/" + pending["task_id"]).json() == pending_snapshot
            assert browser.get("/api/v1/control").json() == paused
            assert browser.get("/api/v1/memories").json()["memories"] == [memory]
            assert browser.get("/api/v1/records/projects").json()["projects"] == [project]
            node = spawn("nexus.node", restored_node_config, log, token=world.node_token.get_secret_value())
            after_node = wait_for(lambda: next((n for n in browser.get("/api/v1/nodes").json()["nodes"] if n["online"]), None))
            assert after_node["tools"] == before_node["tools"] == ["filesystem.list@1", "system.info@1"]
            assert after_node["roots"] == before_node["roots"] == ["workspace"]
            assert after_node["session_id"] != before_node["session_id"]
            assert after_node["boot_id"] != before_node["boot_id"]
            approval_id = pending_snapshot["approvals"][0]["approval_id"]
            approved = browser.post("/api/v1/approvals/" + approval_id, headers=auth, json={"decision": "approve"})
            assert approved.status_code == 200 and approved.json()["task"]["status"] == "ready"
            assert browser.get("/api/v1/tasks/" + pending["task_id"]).json()["attempts"] == []
            assert not browser.post("/api/v1/control/resume", headers=auth, json={"request_id": str(uuid4())}).json()["paused"]
            def directory_completed():
                detail = browser.get("/api/v1/tasks/" + pending["task_id"]).json()
                return detail if detail["task"]["status"] == "succeeded" else None
            directory = wait_for(directory_completed)
            assert len(directory["attempts"]) == 1 and len(directory["task"]["result"]["entries"]) == 2
            with closing(sqlite3.connect(restored_core / "core.sqlite3")) as conn:
                assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
                assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
                assert conn.execute("SELECT count(*) FROM task_attempts").fetchone() == (2,)
                assert conn.execute("SELECT count(*) FROM routing_decisions WHERE mode='rule'").fetchone() == (2,)
                assert conn.execute("SELECT count(*) FROM model_calls").fetchone() == (0,)
            with closing(sqlite3.connect(world.db.path)) as original:
                assert original.execute("SELECT count(*) FROM task_attempts").fetchone() == (1,)
            assert checksums["core"] == hashlib.sha256((archive / "core.sqlite3").read_bytes()).hexdigest()
            assert checksums["node"] == hashlib.sha256((archive / "receipts.sqlite3").read_bytes()).hexdigest()
            (tmp_path / "evidence.json").write_text(json.dumps({
                "original_pids": original_pids, "restored_core_pid": core.pid, "restored_node_pid": node.pid,
                "backup_sha256": checksums, "restored_into_fresh_directories": True,
                "node_initially_offline": True, "scope_preserved": True, "paused_preserved": True,
                "history_preserved": True, "pending_approval_preserved": approval_id,
                "system_task": task["task_id"], "directory_task": pending["task_id"],
                "memory_consent": memory["consent_event_id"], "model_calls": 0,
            }, indent=2), encoding="utf-8")
        finally:
            stop(node); stop(core)
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace")
        assert all(token.get_secret_value() not in output for token in (world.owner_token, world.node_token))
