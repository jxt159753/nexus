"""Opt-in real Chromium interaction with synthetic SSE; no external model call."""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest

from test_chat_action_processes import ACTION_SERVER, configure
from test_link_processes import stop, wait_for


@pytest.mark.skipif(not os.environ.get("NEXUS_TEST_NODE"),reason="opt-in bundled Playwright browser check")
def test_m6_browser_project_memory_todo_and_mobile(world,tmp_path):
    config,_,origin=configure(world,tmp_path)
    env={key:value for key,value in os.environ.items() if not key.startswith("NEXUS_")}
    env["DEEPSEEK_API_KEY"]="synthetic-m6-browser-only"
    core=None
    with (tmp_path/"process.log").open("w+b") as log,httpx2.Client(base_url=origin,timeout=3,trust_env=False) as client:
        try:
            core=subprocess.Popen([sys.executable,"-B","-c",ACTION_SERVER,str(config)],env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            wait_for(lambda:client.get("/").status_code==200)
            reply=subprocess.run([os.environ["NEXUS_TEST_NODE"],str(Path(__file__).with_name("m6_browser.cjs"))],
                                 input=json.dumps({"origin":origin,"token":world.owner_token.get_secret_value(),"desktop":str(tmp_path/"desktop.png"),"mobile":str(tmp_path/"mobile.png")}),
                                 text=True,encoding="utf-8",capture_output=True,timeout=120,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            assert reply.returncode==0,reply.stderr
            evidence=json.loads(reply.stdout);assert evidence["passed"]
            (tmp_path/"browser-evidence.json").write_text(json.dumps(evidence,indent=2),encoding="utf-8")
        finally:
            stop(core)
