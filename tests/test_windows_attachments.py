"""Opt-in installed-Windows check for >2 MiB attachment transfer and recovery."""

import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest

from test_chat_attachments_browser import ATTACHMENT_SERVER

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP = ROOT / "var" / "frontend-check" / "installed" / "nexus-windows.exe"


def free_port():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        return reserved.getsockname()[1]


@pytest.mark.skipif(not os.environ.get("NEXUS_TEST_WINDOWS_APP"), reason="opt-in installed Windows app check")
def test_installed_windows_attachment_transfer(world, tmp_path):
    app = Path(os.environ.get("NEXUS_TEST_WINDOWS_APP", str(DEFAULT_APP)))
    installer = Path(os.environ.get("NEXUS_WINDOWS_INSTALLER", str(
        ROOT / "apps" / "windows" / "src-tauri" / "target" / "release" / "bundle" / "nsis" / "NEXUS_0.1.0_x64-setup.exe")))
    assert app.exists(), f"installed app missing: {app}"
    port = free_port()
    cdp_port = free_port()
    origin = f"http://127.0.0.1:{port}"
    config = tmp_path / "core.toml"
    config.write_text(f'''data_dir={json.dumps(str(world.db.path.parent))}
owner_id="{world.owner_id}"
host="127.0.0.1"
port={port}
local_http=true
origins=["https://tauri.localhost", "{origin}"]
[models]
enabled=true
chat_enabled=true
chat_images_enabled=true
''', encoding="utf-8")
    server = tmp_path / "attachment_server.py"
    server.write_text(ATTACHMENT_SERVER, encoding="utf-8")
    work = tmp_path / "windows-work"
    work.mkdir()
    payload = {
        "repo": str(ROOT),
        "python": sys.executable,
        "serverScript": str(server),
        "config": str(config),
        "coreLog": str(tmp_path / "core-windows.log"),
        "origin": origin,
        "token": world.owner_token.get_secret_value(),
        "app": str(app),
        "installerSha256": hashlib.sha256(installer.read_bytes()).hexdigest() if installer.exists() else "missing",
        "cdpPort": str(cdp_port),
        "appData": str(tmp_path / "appdata"),
        "webviewData": str(tmp_path / "webview2"),
        "workDir": str(work),
        "screenshot": str(tmp_path / "windows-attachment-chat.png"),
        "db": str(world.db.path),
    }
    input_file = tmp_path / "windows-input.json"
    input_file.write_text(json.dumps(payload), encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("NEXUS_")}
    env["NEXUS_WINDOWS_INPUT"] = str(input_file)
    env["NEXUS_PLAYWRIGHT"] = str(ROOT / "var" / "attachment-browser" / "node_modules" / "playwright-core")
    with httpx2.Client(base_url=origin, timeout=3, trust_env=False) as client:
        reply = subprocess.run(
            [os.environ.get("NEXUS_TEST_NODE", "node"), str(Path(__file__).with_name("windows_attachment_browser.cjs"))],
            env=env, text=True, encoding="utf-8", capture_output=True, timeout=420,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    assert reply.returncode == 0, reply.stderr
    evidence = json.loads(reply.stdout)
    assert evidence["passed"]
    assert evidence.get("upload_over_2mib"), evidence
    assert evidence.get("bridge_download_sha256"), evidence
    assert evidence.get("sent_with_consent"), evidence
    assert evidence.get("cancel_no_orphan"), evidence
    assert evidence.get("disconnect_failure_shown"), evidence
    assert evidence.get("retry_once"), evidence
    assert evidence.get("restart_keeps_history"), evidence
    assert evidence.get("expired_login_returns_to_login"), evidence
    (tmp_path / "windows-attachment-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
