"""Real browser rendering of Web, Windows bundled UI and legacy failure feedback."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("node") or not (ROOT / "var/attachment-browser/node_modules/playwright-core").exists(),
                    reason="requires existing Node and local Playwright runtime")
def test_failure_feedback_browser():
    result = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("chat_feedback_browser.cjs"))],
                            capture_output=True, text=True, encoding="utf-8", timeout=90,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"]
