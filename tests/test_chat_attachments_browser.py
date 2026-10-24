"""Opt-in real Chromium check of the attachment composer and its consent gate."""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest

from test_link_processes import stop, wait_for


ATTACHMENT_SERVER = r'''
import json,sys,uvicorn,httpx2
from pathlib import Path
from nexus.core.app import create_app
from nexus.core.config import CoreConfig
from nexus.service_config import load_config
config=load_config(Path(sys.argv[1]),CoreConfig,{})
app=create_app(config)
class Stream(httpx2.AsyncByteStream):
    def __init__(self,value): self.value=value
    async def __aiter__(self):
        def event(delta,finish=None,usage=None):
            body={'model':'deepseek-flash','choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
            if usage is not None: body['usage']=usage
            return ('data: '+json.dumps(body,ensure_ascii=False)+'\n\n').encode()
        yield event({'content':'已收到本轮资料。'})
        yield event({},'stop',{'prompt_tokens':100,'completion_tokens':10})
        yield b'data: [DONE]\n\n'
    async def aclose(self): pass
async def simulated(request):
    assert str(request.url)=='https://api.deepseek.com/chat/completions'
    return httpx2.Response(200,headers={'content-type':'text/event-stream'},stream=Stream(json.loads(request.content)))
app.state.chat.provider.transport=httpx2.MockTransport(simulated)
uvicorn.run(app,host=config.host,port=config.port,access_log=False,log_level='warning',ws='websockets-sansio')
'''


def _port():
    import socket
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        return reserved.getsockname()[1]


@pytest.mark.skipif(not os.environ.get("NEXUS_TEST_NODE"), reason="opt-in bundled Playwright browser check")
def test_attachment_composer_browser(world, tmp_path):
    port = _port()
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
chat_enabled=true
chat_images_enabled=true
''', encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("NEXUS_")}
    env["DEEPSEEK_API_KEY"] = "synthetic-attachment-browser-only"
    env["NEXUS_PLAYWRIGHT"] = str(Path(__file__).resolve().parents[1] / "var" / "attachment-browser" / "node_modules" / "playwright-core")
    core = None
    with (tmp_path / "process.log").open("w+b") as log, httpx2.Client(base_url=origin, timeout=3, trust_env=False) as client:
        try:
            core = subprocess.Popen(
                [sys.executable, "-B", "-c", ATTACHMENT_SERVER, str(config)],
                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            wait_for(lambda: client.get("/").status_code == 200)
            reply = subprocess.run(
                [os.environ["NEXUS_TEST_NODE"], str(Path(__file__).with_name("attachment_browser.cjs"))],
                input=json.dumps({
                    "origin": origin,
                    "token": world.owner_token.get_secret_value(),
                    "desktop": str(tmp_path / "attachments-desktop.png"),
                    "mobile": str(tmp_path / "attachments-mobile.png"),
                }),
                env=env, text=True, encoding="utf-8", capture_output=True, timeout=240,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert reply.returncode == 0, reply.stderr
            evidence = json.loads(reply.stdout)
            assert evidence["passed"]
            (tmp_path / "attachment-browser-evidence.json").write_text(
                json.dumps(evidence, indent=2), encoding="utf-8")
        finally:
            stop(core)
