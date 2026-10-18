"""Real Core/Windows Node and tool execution; only DeepSeek HTTP is simulated."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from uuid import uuid4

import httpx2

from test_link_processes import spawn, stop, wait_for
from test_node_link import grant


MODEL_SERVER = r'''
import json, sys, uvicorn, httpx2
from pathlib import Path
from nexus.core.app import create_app
from nexus.core.config import CoreConfig
from nexus.service_config import load_config
config = load_config(Path(sys.argv[1]), CoreConfig, {})
app = create_app(config)
async def simulated_deepseek(request):
    assert str(request.url) == 'https://api.deepseek.com/chat/completions'
    value = json.loads(request.content)
    text = value['messages'][1]['content']
    proposal = {'intent':'list_directory','arguments':{'root_id':'workspace','limit':2}} if 'directory' in text else {'intent':'get_system_info','arguments':{}}
    return httpx2.Response(200,json={'model':'deepseek-flash','usage':{'prompt_tokens':100,'completion_tokens':20},'choices':[{'message':{'content':json.dumps(proposal)},'finish_reason':'stop'}]})
app.state.language.provider.transport = httpx2.MockTransport(simulated_deepseek)
uvicorn.run(app,host=config.host,port=config.port,access_log=False,log_level='warning',ws='websockets-sansio')
'''


def test_language_real_process_tools_approval_and_restart(world, tmp_path):
    grant(world)
    grant(world,"filesystem.list")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1",0))
        port = reserved.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    config = tmp_path / "core.toml"
    config.write_text(f'''data_dir={json.dumps(str(world.db.path.parent))}
owner_id="{world.owner_id}"
host="127.0.0.1"
port={port}
local_http=true
origins=["{origin}"]
[models]
enabled=true
[nodes."{world.node_id}"]
tools=["system.info@1","filesystem.list@1"]
roots=["workspace"]
''',encoding="utf-8")
    node_dir, root = tmp_path / "node", tmp_path / "workspace"
    node_dir.mkdir()
    root.mkdir()
    (root/"example.txt").write_text("not sent to model",encoding="utf-8")
    node_config = tmp_path / "node.toml"
    node_config.write_text(f'''node_id="{world.node_id}"
data_dir={json.dumps(str(node_dir))}
core_url="ws://127.0.0.1:{port}/ws/v1/node"
local_http=true
allowed_tools=["system.info@1","filesystem.list@1"]
[roots]
workspace={json.dumps(str(root))}
''',encoding="utf-8")
    env = {k:v for k,v in os.environ.items() if not k.startswith("NEXUS_")}
    env["DEEPSEEK_API_KEY"] = "simulated-test-key-no-external-api"
    core = node = None
    with (tmp_path/"process.log").open("w+b") as log, httpx2.Client(base_url=origin,timeout=3,trust_env=False) as browser:
        def start():
            return subprocess.Popen([sys.executable,"-B","-c",MODEL_SERVER,str(config)],env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            core = start()
            wait_for(lambda:browser.get("/").status_code==200)
            session = browser.post("/api/v1/session",json={"token":world.owner_token.get_secret_value()},headers={"origin":origin})
            assert session.status_code == 200
            auth = {"origin":origin,"x-csrf-token":session.json()["csrf_token"]}
            node = spawn("nexus.node",node_config,log,token=world.node_token.get_secret_value())
            wait_for(lambda:browser.get("/api/v1/nodes").json()["nodes"][0]["online"])
            details = []
            language_ids = []
            for text in ("show operating system please","show workspace directory please"):
                value = {"request_id":str(uuid4()),"node_id":str(world.node_id),"text":text,"allow_model":True}
                item = browser.post("/api/v1/language",json=value,headers=auth).json()["request"]
                language_ids.append(item["language_id"])
                path = "/api/v1/language/"+item["language_id"]
                def proposed():
                    item = browser.get(path).json()["request"]
                    return item if item["status"] == "proposed" else None
                wait_for(proposed)
                # Repeated submission never makes a second remote request.
                assert browser.post("/api/v1/language",json=value,headers=auth).json()["request"]["language_id"] == item["language_id"]
                task = browser.post(path+"/confirm",json={},headers=auth).json()["task"]
                if "directory" in text:
                    assert task["status"] == "awaiting_approval"
                    detail = browser.get("/api/v1/tasks/"+task["task_id"]).json()
                    assert detail["attempts"] == []
                    assert browser.post("/api/v1/approvals/"+detail["approvals"][0]["approval_id"],json={"decision":"approve"},headers=auth).status_code==200
                def completed():
                    detail = browser.get("/api/v1/tasks/"+task["task_id"]).json()
                    return detail if detail["task"]["status"] == "succeeded" else None
                detail = wait_for(completed)
                assert detail["task"]["verification"] == "node_report_schema_checked" and len(detail["attempts"]) == 1
                details.append(detail)
            assert details[0]["task"]["result"]["os_name"] == "Windows"
            assert details[1]["task"]["result"]["entries"] == [{"name":"example.txt","kind":"file"}]
            usage = browser.get("/api/v1/models/usage").json()
            assert usage["calls"] == 2 and usage["cost_microunits"] == 108
            first_pid = core.pid
            stop(core)
            core = start()
            wait_for(lambda:browser.get("/api/v1/session").status_code == 200)
            assert core.pid != first_pid
            assert browser.get("/api/v1/models/usage").json() == usage
            for language_id,detail in zip(language_ids,details):
                task = browser.post("/api/v1/language/"+language_id+"/confirm",json={},headers=auth).json()["task"]
                assert task == detail["task"]
            with closing(sqlite3.connect(world.db.path)) as conn:
                assert conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 2
                assert conn.execute("SELECT count(*) FROM task_attempts").fetchone()[0] == 2
                assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            (tmp_path/"m2-evidence.json").write_text(json.dumps({"core_pids":[first_pid,core.pid],"node_pid":node.pid,"tasks":[d["task"]["task_id"] for d in details],"model_calls":2,"simulated_provider":True,"cost_upper_microunits":108,"real_tools":True},indent=2),encoding="utf-8")
        finally:
            stop(node)
            stop(core)
        log.seek(0)
        output = log.read().decode("utf-8",errors="replace")
        assert world.owner_token.get_secret_value() not in output and world.node_token.get_secret_value() not in output
        assert env["DEEPSEEK_API_KEY"] not in output
