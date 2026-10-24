"""Real Core/Windows Node; actual SSE parser uses a bounded synthetic exchange."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from uuid import uuid4

import httpx2

from test_chat_action_processes import configure
from test_link_processes import spawn, stop, wait_for


SKILL_SERVER = r'''
import asyncio,json,sys,uvicorn,httpx2
from pathlib import Path
from nexus.core.app import create_app
from nexus.core.config import CoreConfig
from nexus.service_config import load_config
config=load_config(Path(sys.argv[1]),CoreConfig,{})
app=create_app(config)
def event(delta,finish=None):
    value={'model':'deepseek-flash','choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
    if finish:value['usage']={'prompt_tokens':200,'completion_tokens':30}
    return ('data: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode()
class Stream(httpx2.AsyncByteStream):
    def __init__(self,value):self.value=value
    async def __aiter__(self):
        value=self.value
        title='{"title":"工作区概览"}\n' if 'Start your content' in value['messages'][0]['content'] else ''
        if value['messages'][-1]['role']=='tool':
            assert 'tools' not in value
            data=json.loads(value['messages'][-1]['content'])
            assert data['verification']=='skill_steps_verified'
            assert data['counts_scope']=='returned_entries_only'
            assert value['messages'][-1]['tool_call_id']=='m5-native'
            yield event({'content':'根据实际步骤结果，已完成工作区概览。'})
            yield event({},'stop')
        elif any(t['function']['name']=='run_skill' for t in value.get('tools',[])):
            tool=next(t['function'] for t in value['tools'] if t['function']['name']=='run_skill')
            identifier=tool['parameters']['properties']['version_id']['enum'][0]
            arguments=json.dumps({'version_id':identifier,'input':{}})
            yield event({'content':title+'已选择已启用流程，请核对参数与步骤。'})
            yield event({'tool_calls':[{'index':0,'id':'m5-native','type':'function','function':{'name':'run_skill','arguments':arguments[:10]}}]})
            await asyncio.sleep(.05)
            yield event({'tool_calls':[{'index':0,'function':{'arguments':arguments[10:]}}]})
            yield event({},'tool_calls')
        else:
            yield event({'content':title+'没有匹配的已启用流程，请在能力页面查看缺口。'})
            yield event({},'stop')
        yield b'data: [DONE]\n\n'
    async def aclose(self):pass
async def simulated(request):
    assert str(request.url)=='https://api.deepseek.com/chat/completions'
    return httpx2.Response(200,headers={'content-type':'text/event-stream'},stream=Stream(json.loads(request.content)))
app.state.chat.provider.transport=httpx2.MockTransport(simulated)
uvicorn.run(app,host=config.host,port=config.port,access_log=False,log_level='warning',ws='websockets-sansio')
'''


def start_core(config, log):
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEXUS_")}
    env["DEEPSEEK_API_KEY"] = "synthetic-m5-only-no-external-api"
    return subprocess.Popen([sys.executable, "-B", "-c", SKILL_SERVER, str(config)], env=env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)


def enable_skill(client, auth, node_id):
    def post(path, body):
        reply = client.post("/api/v1/extensions" + path, json=body, headers=auth)
        assert reply.status_code == 200, reply.text
        return reply.json()
    request = post("/requests", {"request_id": str(uuid4()), "kind": "skill", "need": "工作区概览", "node_id": str(node_id)})["request"]
    post("/requests/" + request["request_id"] + "/generate", {"preview_digest": request["preview_digest"], "allow_model": True})
    candidate = wait_for(lambda: (r if (r := client.get("/api/v1/extensions/requests/" + request["request_id"]).json()["request"])["status"] == "candidate" else None))
    path = "/versions/" + candidate["candidate_version_id"]
    tested = post(path + "/test", {})["version"]
    post(path + "/approve", {"decision": "approve", "artifact_digest": tested["artifact_digest"], "test_id": tested["test"]["test_id"]})
    return post(path + "/enable", {})["version"]


def test_skill_chat_real_windows_steps_restart_history_and_final(world, tmp_path):
    config, node_config, origin = configure(world, tmp_path)
    with config.open("a", encoding="utf-8") as file:
        file.write("\n[extensions]\nenabled=true\nfixture_generation=true\n")
    core = node = None
    with (tmp_path / "process.log").open("w+b") as log, httpx2.Client(base_url=origin, timeout=3, trust_env=False) as client:
        try:
            core = start_core(config, log)
            wait_for(lambda: client.get("/").status_code == 200)
            def login():
                response = client.post("/api/v1/session", json={"token": world.owner_token.get_secret_value()}, headers={"origin": origin})
                assert response.status_code == 200
                return {"origin": origin, "x-csrf-token": response.json()["csrf_token"]}
            auth = login()
            node = spawn("nexus.node", node_config, log, token=world.node_token.get_secret_value())
            wait_for(lambda: client.get("/api/v1/nodes").json()["nodes"][0]["online"])
            version = enable_skill(client, auth, world.node_id)
            directory = client.get("/api/v1/chat/skills", params={"node_id": str(world.node_id)}).json()
            assert directory["skills"][0]["version_id"] == version["version_id"]
            project = client.post("/api/v1/projects", json={"name": "M6 workflow project"}, headers=auth).json()["project"]
            conv = client.post("/api/v1/chat/conversations", json={"request_id": str(uuid4()), "project_id": project["project_id"]}, headers=auth).json()["conversation"]
            history_path = "/api/v1/chat/conversations/" + conv["conversation_id"] + "/turns"
            payload = {"request_id": str(uuid4()), "expected_revision": conv["revision"], "text": "给我做一份工作区概览", "allow_model": True,
                       "allow_tools": True, "allow_skills": True, "skills_digest": directory["skills_digest"], "node_id": str(world.node_id)}
            turn = client.post(history_path, json=payload, headers=auth).json()["turn"]
            turn_path = "/api/v1/chat/turns/" + turn["turn_id"]
            proposed = wait_for(lambda: (r if (r := client.get(turn_path).json()["turn"])["action_status"] == "proposed" else None))
            assert proposed["skill_plan"]["ready"] and client.get("/api/v1/extensions/runs").json()["runs"] == []
            decision = {"decision": "confirm", "proposal_digest": proposed["proposal_digest"]}
            started = client.post(turn_path + "/action", json=decision, headers=auth).json()["skill_run"]
            assert client.post(turn_path + "/action", json=decision, headers=auth).json()["skill_run"]["run_id"] == started["run_id"]
            def waiting_directory():
                r = client.get(turn_path).json()["turn"]["skill_run"]
                return r if r["steps"][0]["status"] == "succeeded" and r["steps"][1]["status"] == "approval" else None
            waiting = wait_for(waiting_directory)
            task_id = waiting["steps"][1]["task_id"]
            assert client.get("/api/v1/tasks/" + task_id).json()["attempts"] == []
            old_pid = core.pid
            stop(core); core = start_core(config, log)
            wait_for(lambda: client.get("/api/v1/session").status_code == 200)
            auth = login()
            restored = wait_for(waiting_directory)
            assert [s["task_id"] for s in waiting["steps"]] == [s["task_id"] for s in restored["steps"]]
            detail = client.get("/api/v1/tasks/" + task_id).json()
            assert client.post("/api/v1/approvals/" + detail["approvals"][0]["approval_id"], json={"decision": "approve"}, headers=auth).status_code == 200
            done = wait_for(lambda: (r if (r := client.get(turn_path).json()["turn"])["skill_run"]["status"] == "succeeded" else None))
            assert done["skill_run"]["result"]["system"]["os_name"] == "Windows"
            assert done["skill_run"]["result"]["directory"]["entries"] == [{"name": "example.txt", "kind": "file"}]
            overview = client.get("/api/v1/projects/" + project["project_id"]).json()
            assert len(overview["tasks"]) == 2 and all(task["project_id"] == project["project_id"] for task in overview["tasks"])
            preview = client.post(turn_path + "/result-preview", json={}, headers=auth).json()["preview"]
            consent = {key: preview[key] for key in ("result_digest", "expected_revision")}; consent["allow_model"] = True
            assert client.post(turn_path + "/result-consent", json=consent, headers=auth).status_code == 200
            final = wait_for(lambda: (r if (r := client.get(turn_path).json()["turn"])["phase"] == "summary" and r["status"] == "completed" else None))
            assert final["model_call_count"] == 2 and final["skill_run"]["status"] == "succeeded"
            assert "实际步骤" in final["response_text"] and core.pid != old_pid
            history = client.get(history_path).json()
            assert history["active_turn_id"] is None and history["turns"][0]["skill_run_id"] == started["run_id"]
            with closing(sqlite3.connect(world.db.path)) as conn:
                assert conn.execute("SELECT count(*) FROM skill_runs").fetchone()[0] == 1
                assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
                assert conn.execute("SELECT count(*) FROM task_attempts").fetchone()[0] == 2
                assert conn.execute("SELECT count(*) FROM model_calls WHERE skill_run_id=?", (started["run_id"],)).fetchone()[0] == 2
            (tmp_path / "evidence.json").write_text(json.dumps({"turn": final, "core_restarted": True, "tasks": 2, "attempts": 2, "real_model_calls": 0}, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            stop(node); stop(core)
        log.seek(0)
        output = log.read().decode("utf-8", "replace")
        assert world.owner_token.get_secret_value() not in output and world.node_token.get_secret_value() not in output
