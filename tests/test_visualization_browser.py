"""Animation continuity and camera pivots in the real WebGL browser scene."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    not shutil.which("node") or not (ROOT / "var/attachment-browser/node_modules/playwright-core").exists(),
    reason="requires existing Node and local Playwright runtime",
)
def test_visualization_browser():
    result = subprocess.run(
        [shutil.which("node"), str(Path(__file__).with_name("visualization_browser.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"]


def test_windows_visualization_matches_web():
    assert (ROOT / "src/nexus/web/app/visualization.js").read_bytes() == (
        ROOT / "apps/windows/ui/visualization.js"
    ).read_bytes()
