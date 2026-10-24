"""Independent Core/Windows Node image dispatch over actual HTTP/WebSocket."""

import asyncio
import base64
import io
import json
import os
import socket
import sqlite3
from uuid import uuid4

import httpx2
import pytest

from test_link_processes import spawn, stop, wait_for


@pytest.mark.skipif(os.name != "nt", reason="Windows Node process and Job lifecycle")
def test_image_http_websocket_ack_idempotency_and_core_restart(world,tmp_path):
    Image = pytest.importorskip("PIL.Image")
    async def grants():
        for identity in (world.owner_id,world.node_id):
            await world.security.set_grant(identity,"filesystem.read_image",root_id="workspace")
    asyncio.run(grants())
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1",0)); port = reserved.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    root = tmp_path/"workspace"; root.mkdir()
    node_dir = tmp_path/"node"; node_dir.mkdir()
    Image.new("RGB",(120,60),"red").save(root/"test.png")
    core_config = tmp_path/"core.toml"
    core_config.write_text(f'''data_dir={json.dumps(str(world.db.path.parent))}
owner_id="{world.owner_id}"
host="127.0.0.1"
port={port}
local_http=true
origins=["{origin}"]
heartbeat_interval_seconds=1
heartbeat_timeout_seconds=3
[nodes."{world.node_id}"]
tools=["filesystem.read_image@1"]
roots=["workspace"]
''',encoding="utf-8")
    node_config = tmp_path/"node.toml"
    node_config.write_text(f'''node_id="{world.node_id}"
data_dir={json.dumps(str(node_dir))}
core_url="ws://127.0.0.1:{port}/ws/v1/node"
local_http=true
allowed_tools=["filesystem.read_image@1"]
[roots]
workspace={json.dumps(str(root))}
''',encoding="utf-8")
    core = node = None
    with (tmp_path/"process.log").open("w+b") as log,httpx2.Client(base_url=origin,timeout=3,trust_env=False) as client:
        try:
            core = spawn("nexus.core",core_config,log)
            wait_for(lambda:client.get("/api/v1/tasks").status_code == 401)
            response = client.post("/api/v1/session",json={"token":world.owner_token.get_secret_value()},headers={"origin":origin})
            assert response.status_code == 200
            auth = {"origin":origin,"x-csrf-token":response.json()["csrf_token"]}
            node = spawn("nexus.node",node_config,log,token=world.node_token.get_secret_value())
            wait_for(lambda:client.get("/api/v1/nodes").json()["nodes"][0]["online"])
            request = {"node_id":str(world.node_id),"request":{"request_id":str(uuid4()),"intent":"read_image","arguments":{"root_id":"workspace","path":"test.png"}}}
            response = client.post("/api/v1/tasks",json=request,headers=auth)
            assert response.status_code == 200,response.text
            task_id = response.json()["task"]["task_id"]
            def settled():
                detail = client.get(f"/api/v1/tasks/{task_id}").json()
                return detail if detail["task"]["status"] == "succeeded" else None
            result = wait_for(settled)
            assert len(result["attempts"]) == 1 and result["task"]["verification"] == "node_report_schema_checked"
            image = result["task"]["result"]["image"]
            with Image.open(io.BytesIO(base64.b64decode(image["data_base64"]))) as decoded:
                assert decoded.size == (120,60) and decoded.getpixel((0,0)) == (255,0,0)
            (root/"test.png").unlink()
            assert client.post("/api/v1/tasks",json=request,headers=auth).json()["task"]["task_id"] == task_id
            def acknowledged():
                with sqlite3.connect(node_dir/"receipts.sqlite3") as conn:
                    return conn.execute("SELECT count(*) FROM execution_receipts WHERE acknowledged=1").fetchone()[0] == 1
            wait_for(acknowledged)
            stop(core); core = spawn("nexus.core",core_config,log)
            wait_for(lambda:client.get("/api/v1/tasks").status_code == 200)
            wait_for(lambda:client.get("/api/v1/nodes").json()["nodes"][0]["online"])
            assert client.get(f"/api/v1/tasks/{task_id}").json() == result
            assert client.get("/api/v1/models/usage").json()["calls"] == 0
        finally:
            stop(node); stop(core)
