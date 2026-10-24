"""Autonomy owner UI wiring and real-browser checks on both packaged surfaces."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexus.core.app import create_app
from test_node_link import ORIGIN, core_config

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "nexus" / "web" / "app"
WINDOWS = ROOT / "apps" / "windows" / "ui"


def test_app_serves_owner_autonomy_assets_and_entry(world):
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        page = client.get("/app")
        assert page.status_code == 200
        html = page.text
        assert 'id="notifications-button"' in html
        assert "app.css?v=20261023a" in html and "main.js?v=20261023a" in html
        for name in ("autonomy.js", "autonomy-page.js", "plugin-page.js"):
            reply = client.get(f"/static/app/{name}")
            assert reply.status_code == 200
            assert "javascript" in reply.headers.get("content-type", "")
        css = client.get("/static/app/app.css").text
        for marker in (".autonomy-form", ".autonomy-event-value", ".autonomy-verification"):
            assert marker in css, marker


def test_frontend_wiring_uses_documented_autonomy_endpoints_and_revisions():
    source = (UI / "autonomy.js").read_text(encoding="utf-8")
    for marker in (
        "/api/v1/autonomy/health",
        "/api/v1/autonomy/events?after=",
        "/api/v1/autonomy/watches",
        "/ack",
        "/goals/",
        "/policy",
        "/business-suite",
        "/archive-preview",
        "/archive",
        "archived=true",
        "expected_revision",
        "writeEventCursor",
        "readSeenAlerts",
        "WATCH_ENDED",
        "EXECUTION_UNKNOWN",
        "ARCHIVE_INVALID",
    ):
        assert marker in source, marker
    page = (UI / "autonomy-page.js").read_text(encoding="utf-8")
    for marker in (
        "机器断言通过",
        "用户验收：未记录",
        "重启后自动续跑（默认关闭）",
        "策略 revision",
        "watch_id",
        "event_id",
        "digest",
        "host_safety_verified=false",
        "编辑器中的候选用例与开关仍保留",
        "textContent",
        "不执行",
    ):
        assert marker in page, marker
    # Event, case and archive content is written as text, never as HTML.
    assert ".innerHTML" not in page and ".insertAdjacentHTML" not in page
    plugin = (UI / "plugin-page.js").read_text(encoding="utf-8")
    for marker in ("观察订阅", "通知中心", "存储与健康", "查看目标详情", "独立业务用例与回退"):
        assert marker in plugin, marker


def test_windows_bundle_keeps_the_same_autonomy_semantics():
    for name in ("autonomy.js", "autonomy-page.js", "plugin-page.js", "plugins.js", "render.js"):
        assert (WINDOWS / name).is_file(), name
    assert (UI / "autonomy.js").read_text(encoding="utf-8") == (WINDOWS / "autonomy.js").read_text(encoding="utf-8")
    assert (UI / "autonomy-page.js").read_text(encoding="utf-8") == (WINDOWS / "autonomy-page.js").read_text(encoding="utf-8")
    assert (UI / "plugin-page.js").read_text(encoding="utf-8") == (WINDOWS / "plugin-page.js").read_text(encoding="utf-8")
    windows_page = (WINDOWS / "app.html").read_text(encoding="utf-8")
    assert 'id="notifications-button"' in windows_page
    assert "20261023a" in windows_page
    assert "renderTaskCard" in (WINDOWS / "render.js").read_text(encoding="utf-8")
    windows_main = (WINDOWS / "main.js").read_text(encoding="utf-8")
    assert 'openPlugins("events")' in windows_main
    windows_css = (WINDOWS / "app.css").read_text(encoding="utf-8")
    assert ".autonomy-verification" in windows_css


@pytest.mark.skipif(
    not shutil.which("node") or not (ROOT / "var/attachment-browser/node_modules/playwright-core").exists(),
    reason="requires existing Node and local Playwright runtime",
)
def test_autonomy_ui_in_real_browser():
    result = subprocess.run(
        [shutil.which("node"), str(Path(__file__).with_name("autonomy_browser.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr[-4000:]
    value = json.loads(result.stdout)
    assert value["passed"] is True
    assert [item["surface"] for item in value["surfaces"]] == ["web", "windows"]
    assert value["real_model_calls"] == 0
