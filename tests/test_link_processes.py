"""Real loopback sockets and two independent application processes."""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx2
from pydantic import SecretStr
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


def wait_for(check, *, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (httpx2.HTTPError, OSError, KeyError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError("controlled Core/Node condition was not observed")


def spawn(module, config, log, *, token=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("NEXUS_")}
    if token is not None:
        env["NEXUS_NODE_TOKEN"] = token
    return subprocess.Popen([sys.executable, "-B", "-m", module, "--config", str(config)],
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW)


def stop(process):
    if process is not None:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def test_independent_core_node_reconnect_reauthenticate_and_rotate(world, tmp_path):
    from nexus.contracts.task import SystemInfoArguments, SystemInfoRequest
    task = asyncio.run(world.tasks.create(world.owner_id, SystemInfoRequest(request_id=uuid4(), intent="get_system_info", arguments=SystemInfoArguments()), world.node_id))
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
heartbeat_timeout_seconds=2
[nodes."{world.node_id}"]
tools=["system.info@1"]
roots=[]
''', encoding="utf-8")
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    node_config = tmp_path / "node.toml"
    node_config.write_text(f'''node_id="{world.node_id}"
data_dir={json.dumps(str(node_dir))}
core_url="ws://127.0.0.1:{port}/ws/v1/node"
local_http=true
allowed_tools=["system.info@1"]
''', encoding="utf-8")
    core_process, node_process = None, None
    log_path = tmp_path / "process.log"
    with log_path.open("w+b") as log, httpx2.Client(base_url=origin, timeout=2, trust_env=False) as browser:
        try:
            core_process = spawn("nexus.core", core_config, log)
            wait_for(lambda: browser.get("/api/v1/nodes").status_code == 401)
            assert browser.post("/api/v1/session", json={"token": world.owner_token.get_secret_value()}, headers={"origin": origin}).status_code == 200
            node_process = spawn("nexus.node", node_config, log, token=world.node_token.get_secret_value())
            def online():
                node = browser.get("/api/v1/nodes").json()["nodes"][0]
                return node if node["online"] else None
            first = wait_for(online)
            assert node_process.pid != core_process.pid
            assert first["tools"] == []  # Registration does not manufacture grants.
            time.sleep(2.2)
            assert online()  # More than one heartbeat timeout has passed.
            assert core_process.poll() is None and node_process.poll() is None
            old_core_pid = core_process.pid
            stop(core_process)
            core_process = None
            assert node_process.poll() is None
            core_process = spawn("nexus.core", core_config, log)
            second = wait_for(online)
            assert core_process.pid != old_core_pid
            assert second["session_id"] != first["session_id"]
            assert second["boot_id"] == first["boot_id"]
            new_token = SecretStr(secrets.token_urlsafe(32))
            asyncio.run(world.security.rotate_credential(world.node_id, new_token))
            wait_for(lambda: not browser.get("/api/v1/nodes").json()["nodes"][0]["online"])
            async def old_token_rejected():
                try:
                    async with connect(f"ws://127.0.0.1:{port}/ws/v1/node", additional_headers={"Authorization": "Bearer " + world.node_token.get_secret_value()}, proxy=None):
                        raise AssertionError("old credential unexpectedly authenticated")
                except InvalidStatus as exc:
                    assert exc.response.status_code == 403
            asyncio.run(old_token_rejected())
            stop(node_process)
            node_process = spawn("nexus.node", node_config, log, token=new_token.get_secret_value())
            third = wait_for(online)
            assert third["boot_id"] != second["boot_id"]
            assert third["session_id"] != second["session_id"]
            assert asyncio.run(world.tasks.get(task.task_id, world.owner_id)) == task
        finally:
            stop(node_process)
            stop(core_process)
        log.seek(0)
        output = log.read(65536).decode("utf-8", errors="replace")
        for token in (world.owner_token, world.node_token, new_token):
            assert token.get_secret_value() not in output
