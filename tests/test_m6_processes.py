"""Independent Core and Windows Node: effects, approvals, cancellation, restart."""

import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest

from nexus.node.config import CommandTemplate
from nexus.node.windows import job_stopped
from test_link_processes import spawn, stop, wait_for


@pytest.mark.skipif(os.name!="nt",reason="Windows Node and Job evidence")
def test_m6_basic_tools_real_process_approval_effect_cancel_restart(world,tmp_path):
    tools=["filesystem.read","filesystem.search","filesystem.write_preview","filesystem.write","command.run"]
    async def grants():
        for tool in tools:
            for identity in (world.owner_id,world.node_id):
                await world.security.set_grant(identity,tool,root_id="workspace")
    asyncio.run(grants())
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    origin=f"http://127.0.0.1:{port}"
    root=tmp_path/"workspace";root.mkdir();(root/"note.txt").write_text("before",encoding="utf-8")
    node_dir=tmp_path/"node";node_dir.mkdir()
    exe=Path(sys.executable);exe_hash=hashlib.sha256(exe.read_bytes()).hexdigest()
    definitions={"echo": ["-I","-c","print('controlled-ok')"],"slow":["-I","-c","import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)']); open('owned-child.pid','w').write(str(p.pid)); time.sleep(60)"]}
    templates={key:CommandTemplate(executable=exe,executable_sha256=exe_hash,argv=argv) for key,argv in definitions.items()}
    digests={key:value.digest() for key,value in templates.items()}
    core_config=tmp_path/"core.toml"
    core_config.write_text(f'''data_dir={json.dumps(str(world.db.path.parent))}
owner_id="{world.owner_id}"
host="127.0.0.1"
port={port}
local_http=true
origins=["{origin}"]
heartbeat_interval_seconds=1
heartbeat_timeout_seconds=3
[nodes."{world.node_id}"]
tools={json.dumps([tool+'@1' for tool in tools])}
roots=["workspace"]
[nodes."{world.node_id}".command_digests]
echo="{digests['echo']}"
slow="{digests['slow']}"
''',encoding="utf-8")
    node_config=tmp_path/"node.toml"
    node_config.write_text(f'''node_id="{world.node_id}"
data_dir={json.dumps(str(node_dir))}
core_url="ws://127.0.0.1:{port}/ws/v1/node"
local_http=true
allowed_tools={json.dumps([tool+'@1' for tool in tools])}
[roots]
workspace={json.dumps(str(root))}
'''+"".join(f'\n[commands.{key}]\nexecutable={json.dumps(str(exe))}\nexecutable_sha256="{exe_hash}"\nargv={json.dumps(value)}\n' for key,value in definitions.items()),encoding="utf-8")
    core=node=None
    with (tmp_path/"process.log").open("w+b") as log,httpx2.Client(base_url=origin,timeout=3,trust_env=False) as client:
        try:
            core=spawn("nexus.core",core_config,log)
            wait_for(lambda:client.get("/").status_code==200)
            def login():
                reply=client.post("/api/v1/session",json={"token":world.owner_token.get_secret_value()},headers={"origin":origin})
                assert reply.status_code==200
                return {"origin":origin,"x-csrf-token":reply.json()["csrf_token"]}
            auth=login()
            node=spawn("nexus.node",node_config,log,token=world.node_token.get_secret_value())
            wait_for(lambda:client.get("/api/v1/nodes").json()["nodes"][0]["online"])
            assert client.get("/api/v1/nodes").json()["nodes"][0]["command_digests"]==digests
            project=client.post("/api/v1/projects",json={"name":"M6 real"},headers=auth).json()["project"]["project_id"]
            def create(intent,args):
                request={"node_id":str(world.node_id),"request":{"request_id":str(uuid4()),"intent":intent,"arguments":args,"project_id":project}}
                reply=client.post("/api/v1/tasks",json=request,headers=auth)
                assert reply.status_code==200,reply.text
                task=reply.json()["task"]
                assert task["status"]=="awaiting_approval"
                assert client.post("/api/v1/tasks",json=request,headers=auth).json()["task"]["task_id"]==task["task_id"]
                detail=client.get(f"/api/v1/tasks/{task['task_id']}").json()
                assert detail["attempts"]==[]
                return task,detail["approvals"][0]["approval_id"]
            def approve(approval):
                reply=client.post(f"/api/v1/approvals/{approval}",json={"decision":"approve"},headers=auth)
                assert reply.status_code==200,reply.text
            def settled(task_id,status="succeeded"):
                detail=client.get(f"/api/v1/tasks/{task_id}").json()
                return detail if detail["task"]["status"]==status else None
            read,approval=create("read_file",{"root_id":"workspace","path":"note.txt"})
            approve(approval);result=wait_for(lambda:settled(read["task_id"]))
            assert result["task"]["result"]["text"]=="before"
            args={"root_id":"workspace","path":"note.txt","content":"after\n"}
            preview,approval=create("preview_file_write",args)
            approve(approval);data=wait_for(lambda:settled(preview["task_id"]))["task"]["result"]
            assert "-before" in data["diff"] and "+after" in data["diff"]
            write,approval=create("write_file",{**args,"expected_sha256":data["previous_sha256"],"preview_task_id":preview["task_id"],"preview_digest":data["preview_digest"]})
            assert (root/"note.txt").read_text()=="before"
            approve(approval);written=wait_for(lambda:settled(write["task_id"]))
            assert (root/"note.txt").read_bytes()==b"after\n" and len(written["attempts"])==1
            search,approval=create("find_files",{"root_id":"workspace","pattern":"*.txt"})
            approve(approval);assert wait_for(lambda:settled(search["task_id"]))["task"]["result"]["paths"]==["note.txt"]
            command,approval=create("run_command",{"root_id":"workspace","command_id":"echo","template_digest":digests["echo"]})
            approve(approval);assert wait_for(lambda:settled(command["task_id"]))["task"]["result"]["stdout"].strip()=="controlled-ok"
            slow,approval=create("run_command",{"root_id":"workspace","command_id":"slow","template_digest":digests["slow"]})
            approve(approval);wait_for(lambda:(root/"owned-child.pid").exists())
            reply=client.post(f"/api/v1/tasks/{slow['task_id']}/cancel",json={},headers=auth);assert reply.status_code==200
            cancelled=wait_for(lambda:settled(slow["task_id"],"cancelled"))
            assert len(cancelled["attempts"])==1
            def stopped_job():
                with closing(sqlite3.connect(node_dir/"receipts.sqlite3")) as conn:
                    job=conn.execute("SELECT job_name FROM execution_receipts WHERE task_id=?",(slow["task_id"],)).fetchone()
                return job and job_stopped(job[0])
            wait_for(stopped_job)
            overview=client.get(f"/api/v1/projects/{project}").json();assert len(overview["tasks"])==6
            stop(core);core=spawn("nexus.core",core_config,log)
            wait_for(lambda:client.get("/").status_code==200);auth=login()
            assert client.get(f"/api/v1/tasks/{write['task_id']}").json()==written
            assert client.get(f"/api/v1/projects/{project}").json()["task_counts"]["succeeded"]==5
            assert client.get("/api/v1/models/usage").json()["calls"]==0
        finally:
            stop(node);stop(core)
