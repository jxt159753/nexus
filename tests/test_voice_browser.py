"""Real Edge UI against a real temporary Core; native media is an explicit fake."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import httpx2
import pytest

from test_link_processes import stop, wait_for


@pytest.mark.skipif(not os.environ.get("NEXUS_TEST_NODE"), reason="opt-in browser check")
@pytest.mark.parametrize("script", ["voice_browser.cjs", "voice_resident_browser.cjs"])
def test_voice_consent_and_control_browser(world, tmp_path, script):
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    config = tmp_path / "core.toml"
    config.write_text(f'''data_dir={json.dumps(str(world.db.path.parent))}
owner_id="{world.owner_id}"
local_http=true
port={port}
origins=["{origin}"]
[voice]
enabled=true
provider="fake"
[nodes."{world.node_id}".voice]
enabled=true
features=["capture","playback","wake"]
''', encoding="utf-8")
    core = None
    env = os.environ.copy()
    env["NEXUS_PLAYWRIGHT"] = str(root / "var/attachment-browser/node_modules/playwright-core")
    with (tmp_path / "core.log").open("w+b") as log, httpx2.Client(base_url=origin, trust_env=False, timeout=2) as client:
        try:
            core = subprocess.Popen([sys.executable, "-B", "-m", "nexus.core", "--config", str(config)],
                                    cwd=root, stdout=log, stderr=log, env=env)
            wait_for(lambda: client.get("/").status_code == 200)
            result = subprocess.run([os.environ["NEXUS_TEST_NODE"], str(root / "tests" / script)],
                input=json.dumps({"origin": origin, "node_id": str(world.node_id),
                                  "owner_token": world.owner_token.get_secret_value(),
                                  "screenshot": str(tmp_path / "voice-390.png")}),
                text=True, encoding="utf-8", capture_output=True, env=env, timeout=75)
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout.strip().splitlines()[-1])["passed"]
        finally:
            stop(core)
