"""Owner controls, real sockets and independent-process fault injection for S5."""

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient

from nexus.contracts.messages import CommandStatusResult
from nexus.contracts.protocol import parse_message
from nexus.core.app import create_app
from nexus.core.link.nodes import NodeConnection
from nexus.link import wire_message
from node_support import blocking_worker
from test_harness_http import login, submission
from test_link_processes import spawn, stop, wait_for
from test_node_link import ORIGIN, core_config, grant, handshake, headers, hello
from test_harness import ready
from test_node_runtime import assert_process_dead


def test_control_auth_pause_restart_and_resumed_dispatch(world):
    grant(world)
    config = core_config(world, heartbeat_timeout_seconds=10)
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        assert client.get("/api/v1/control").status_code == 401
        auth = login(client, world)
        assert client.post("/api/v1/control/pause", json={"request_id": str(uuid4())}, headers={"origin": ORIGIN}).status_code == 403
        assert client.post("/api/v1/control/pause", json={"request_id": str(uuid4()), "paused": False}, headers=auth).status_code == 400
        assert client.post("/api/v1/control/pause", json={"request_id": str(uuid4())}, headers=auth).json()["paused"]
        task = client.post("/api/v1/tasks", json=submission(world), headers=auth).json()["task"]
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        auth = login(client, world)
        assert client.get("/api/v1/control").json()["paused"]
        with client.websocket_connect("ws://127.0.0.1:8000/ws/v1/node", headers=headers(world)) as ws:
            handshake(ws, world)
            current = client.get("/api/v1/tasks/" + task["task_id"]).json()
            assert current["attempts"] == []
            assert not client.post("/api/v1/control/resume", json={"request_id": str(uuid4())}, headers=auth).json()["paused"]
            item = parse_message(ws.receive_text()).payload
            assert str(item.task_id) == task["task_id"]
            cancelled = client.post("/api/v1/tasks/" + task["task_id"] + "/cancel", json={}, headers=auth).json()
            assert cancelled["disposition"] == "requested" and cancelled["task"]["status"] != "cancelled"
            message = parse_message(ws.receive_text())
            assert message.type == "command.cancel"
            ws.send_text(wire_message("command.status_result", CommandStatusResult(session_id=item.session_id,
                attempt_id=item.attempt_id, command_digest=item.command_digest, phase="cancelled", execution_stopped=True)))
            wait_for(lambda: client.get("/api/v1/tasks/" + task["task_id"]).json()["task"]["status"] == "cancelled")


def test_pause_fences_a_command_already_in_the_transport_queue(world):
    async def scenario():
        app = create_app(core_config(world))
        harness, link = app.state.harness, app.state.node_link
        task = await ready(world)
        sent = []
        finished = asyncio.Event()
        class Socket:
            async def send_text(self, text):
                sent.append(parse_message(text))
                finished.set()
        connection = NodeConnection(Socket(), world.node, hello(world), world.scope)
        link.connections[world.node_id] = connection
        command = await harness.executions.dispatch(task.task_id, world.node, world.scope, connection.session_id, connection.hello.boot_id, 10)
        await link.dispatch(connection, command)
        await harness.control(world.owner, "pause", uuid4())
        sender = asyncio.create_task(link._sender(connection))
        try:
            async with asyncio.timeout(2):
                while not connection.outgoing.empty():
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
            assert sent == []
            assert await harness.executions.resend(world.node, world.scope, command.attempt_id, connection.session_id) is None
            await harness.control(world.owner, "resume", uuid4())
            retransmit = await harness.executions.resend(world.node, world.scope, command.attempt_id, connection.session_id)
            await link.dispatch(connection, retransmit)
            async with asyncio.timeout(2):
                await finished.wait()
            assert len(sent) == 1 and sent[0].payload.attempt_id == command.attempt_id
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
    asyncio.run(scenario())


def injected_process(module, config, log, token, patch):
    env = {key: value for key, value in os.environ.items() if not key.startswith("NEXUS_")}
    if token:
        env["NEXUS_NODE_TOKEN"] = token
    script = f'from {module}.__main__ import main\n' + patch + '\nmain()\n'
    return subprocess.Popen([sys.executable, "-B", "-c", script, "--config", str(config)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, creationflags=subprocess.CREATE_NO_WINDOW)


@pytest.mark.parametrize("fault", ["before_send", "before_ack", "lost_receipts", "after_result_disconnect", "node_crash", "cancel", "timeout"])
def test_independent_process_fault_recovery_and_real_stop(world, tmp_path, fault):
    grant(world)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    core_config_path = tmp_path / "core.toml"
    core_config_path.write_text(f'''data_dir={json.dumps(str(world.db.path.parent))}
owner_id="{world.owner_id}"
host="127.0.0.1"
port={port}
local_http=true
origins=["{origin}"]
heartbeat_interval_seconds=1
heartbeat_timeout_seconds=10
execution_timeout_seconds={2 if fault == "timeout" else 10}
[nodes."{world.node_id}"]
tools=["system.info@1"]
''', encoding="utf-8")
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    node_config_path = tmp_path / "node.toml"
    node_config_path.write_text(f'''node_id="{world.node_id}"
data_dir={json.dumps(str(node_dir))}
core_url="ws://127.0.0.1:{port}/ws/v1/node"
local_http=true
allowed_tools=["system.info@1"]
''', encoding="utf-8")
    marker = tmp_path / "fault.marker"
    worker_marker = tmp_path / "worker.pid"
    core, node = None, None
    old_core_pid, old_node_pid = None, None
    core_patch = ""
    node_patch = ""
    if fault in {"before_send", "before_ack"}:
        kind = "command.execute" if fault == "before_send" else "result.ack"
        core_patch = f'''
import os
from pathlib import Path
from nexus.core.link.nodes import NodeLink
original = NodeLink._enqueue
async def inject(self, connection, kind, payload):
    if kind == {kind!r}:
        Path({str(marker)!r}).write_text(str(os.getpid()))
        os._exit(72)
    await original(self, connection, kind, payload)
NodeLink._enqueue = inject
'''
    if fault == "lost_receipts":
        node_patch = '''
from nexus.node.link import NodeClient
original = NodeClient._send
async def drop(self, ws, kind, payload, **kwargs):
    if kind in {"command.accepted", "command.started", "command.result"}:
        return
    await original(self, ws, kind, payload, **kwargs)
NodeClient._send = drop
'''
    if fault == "after_result_disconnect":
        node_patch = f'''
from pathlib import Path
from nexus.node.link import NodeClient
original = NodeClient._send
async def disconnect(self, ws, kind, payload, **kwargs):
    marker = Path({str(marker)!r})
    if kind == "command.result" and not marker.exists():
        marker.write_text("result persisted; disconnect before Core receives it")
        await ws.close()
        return
    await original(self, ws, kind, payload, **kwargs)
NodeClient._send = disconnect
'''
    if fault in {"node_crash", "cancel", "timeout"}:
        node_patch = f'''
import subprocess, sys
from nexus.node import process as process_module
original = subprocess.Popen
def worker(args, **kwargs):
    return original([sys.executable, "-B", "-c", {blocking_worker(worker_marker)!r}], **kwargs)
process_module.subprocess.Popen = worker
'''
    with (tmp_path / "process.log").open("w+b") as log, httpx2.Client(base_url=origin, timeout=3, trust_env=False) as browser:
        try:
            core = injected_process("nexus.core", core_config_path, log, None, core_patch)
            wait_for(lambda: browser.get("/api/v1/tasks").status_code == 401)
            response = browser.post("/api/v1/session", json={"token": world.owner_token.get_secret_value()}, headers={"origin": origin})
            auth = {"origin": origin, "x-csrf-token": response.json()["csrf_token"]}
            node = injected_process("nexus.node", node_config_path, log, world.node_token.get_secret_value(), node_patch)
            wait_for(lambda: browser.get("/api/v1/nodes").json()["nodes"][0]["online"])
            task = browser.post("/api/v1/tasks", json=submission(world), headers=auth).json()["task"]
            task_id = task["task_id"]
            worker_pid = None
            if fault in {"node_crash", "cancel", "timeout"}:
                wait_for(worker_marker.exists)
                worker_pid = int(worker_marker.read_text())
            if fault in {"before_send", "before_ack"}:
                wait_for(marker.exists)
                core.wait(timeout=5)
                old_core_pid = core.pid
                core = spawn("nexus.core", core_config_path, log)
                wait_for(lambda: browser.get("/api/v1/tasks").status_code == 200)
            if fault == "node_crash":
                old_node_pid = node.pid
                stop(node)
                assert_process_dead(worker_pid)
                node = spawn("nexus.node", node_config_path, log, token=world.node_token.get_secret_value())
            if fault == "cancel":
                result = browser.post("/api/v1/tasks/" + task_id + "/cancel", json={}, headers=auth).json()
                assert result["task"]["cancel_requested"]
            expected = "cancelled" if fault == "cancel" else "failed" if fault == "timeout" else "succeeded"
            def terminal():
                detail = browser.get("/api/v1/tasks/" + task_id).json()
                return detail if detail["task"]["status"] == expected else None
            detail = wait_for(terminal, timeout=20)
            if fault == "after_result_disconnect":
                assert marker.exists()
            if fault == "timeout":
                assert detail["task"]["error"]["code"] == "BUDGET_EXCEEDED"
                assert len(detail["attempts"]) == 2
            elif fault == "node_crash":
                assert len(detail["attempts"]) == 2
            else:
                assert len(detail["attempts"]) == 1
            if worker_pid:
                assert_process_dead(worker_pid)
                assert_process_dead(int(worker_marker.read_text()))
            assert detail["task"]["execution_outcome"] == "known"
            with closing(sqlite3.connect(node_dir / "receipts.sqlite3")) as receipts:
                count = receipts.execute("SELECT count(*) FROM execution_receipts").fetchone()[0]
                assert count == (2 if fault in {"node_crash", "timeout"} else 1)
                if fault == "node_crash":
                    assert receipts.execute("SELECT phase,execution_stopped FROM execution_receipts WHERE attempt_id=?", (detail["attempts"][0]["attempt_id"],)).fetchone() == ("unknown", 1)
            if fault == "before_send":
                assert detail["attempts"][0]["dispatch_count"] == 2
            if fault == "before_ack":
                def acked():
                    with closing(sqlite3.connect(node_dir / "receipts.sqlite3")) as conn:
                        return conn.execute("SELECT acknowledged FROM execution_receipts").fetchone()[0] == 1
                wait_for(acked)
                assert browser.get("/api/v1/tasks/" + task_id).json() == detail
            (tmp_path / "evidence.json").write_text(json.dumps({
                "fault": fault, "core_pid": core.pid, "node_pid": node.pid,
                "old_core_pid": old_core_pid, "old_node_pid": old_node_pid,
                "worker_pid": worker_pid, "receipt_count": count, "detail": detail,
            }, indent=2), encoding="utf-8")
        finally:
            stop(node)
            stop(core)
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace")
        assert world.owner_token.get_secret_value() not in output
        assert world.node_token.get_secret_value() not in output
