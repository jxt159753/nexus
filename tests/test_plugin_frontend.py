"""Native plugin, permission and automation UI wiring stays on real endpoints."""

from pathlib import Path

from fastapi.testclient import TestClient

from nexus.core.app import create_app
from test_node_link import ORIGIN, core_config

ROOT = Path(__file__).resolve().parents[1]


def test_app_plugin_permission_and_automation_assets_are_served(world):
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        page = client.get("/app")
        assert page.status_code == 200
        html = page.text
        for marker in ['id="plugins-button"', 'id="plugin-view"', 'id="plugin-tabs"',
                       'id="plugin-body"', 'id="plugin-close"']:
            assert marker in html, marker
        assert "app.css?v=20261023a" in html and "main.js?v=20261023a" in html
        for name in ("plugins.js", "plugin-page.js", "plugin-view.js"):
            reply = client.get(f"/static/app/{name}")
            assert reply.status_code == 200
            assert "javascript" in reply.headers.get("content-type", "")
        source = client.get("/static/app/plugins.js").text
        for marker in ("/api/v1/permissions/default", "/api/v1/plugins",
                       "/api/v1/plugins/conversations/", "/schedules/", "/goals/",
                       "download_url", "expected_revision"):
            assert marker in source, marker
        page_source = client.get("/static/app/plugin-page.js").text
        for marker in ("effective_mode", "source", "revision",
                       "unconfigured", "node_offline", "bundled", "plugin_install",
                       "Asia/Shanghai", "request_id", "PLUGIN_NOT_TESTED",
                       "IDEMPOTENCY_CONFLICT"):
            assert marker in page_source, marker
        view = client.get("/static/app/plugin-view.js").text
        for marker in ("ask_user_question", "execution_stopped", "job_kill",
                       "terminal_signal", "readDeliverable", "safeTextPreview",
                       "不执行 HTML"):
            assert marker in view, marker
        css = client.get("/static/app/app.css").text
        assert ".plugin-view" in css and ".generation-stage" in css


def test_legacy_console_exposes_plugin_permission_and_automation_views():
    html = (ROOT / "src" / "nexus" / "web" / "index.html").read_text(encoding="utf-8")
    for marker in ('data-tab="plugins"', 'data-tab="automation"', 'id="plugins-view"',
                   'id="automation-view"', 'id="permission-panel"',
                   'id="chat-permission-panel"', 'id="plugin-author-goal"',
                   'id="automation-schedules"', "/static/plugins.js"):
        assert marker in html, marker
    source = (ROOT / "src" / "nexus" / "web" / "plugins.js").read_text(encoding="utf-8")
    for marker in ("/api/v1/permissions/default", "/api/v1/plugins",
                   "/schedules/", "/goals/", "pluginStopProcess",
                   "pluginChatPermissionQuestion", "Asia/Shanghai", "download_url"):
        assert marker in source, marker
    chat = (ROOT / "src" / "nexus" / "web" / "chat-work.js").read_text(encoding="utf-8")
    assert "pluginChatStep" in chat


def test_windows_plugin_ui_and_bridge_use_authenticated_deliverable():
    ui = ROOT / "apps" / "windows" / "ui"
    html = (ui / "app.html").read_text(encoding="utf-8")
    for marker in ('id="plugins-button"', 'id="plugin-view"', 'id="plugin-tabs"'):
        assert marker in html, marker
    for name in ("plugins.js", "plugin-page.js", "plugin-view.js"):
        assert (ui / name).is_file(), name
    bridge = (ROOT / "apps" / "windows" / "ui" / "native-bridge.js").read_text(encoding="utf-8")
    assert "readDeliverable" in bridge and "read_deliverable" in bridge
    rust = (ROOT / "apps" / "windows" / "src-tauri" / "src" / "bridge.rs").read_text(encoding="utf-8")
    for marker in ("read_deliverable", "validate_deliverable_path", "MAX_DELIVERABLE_BYTES",
                   "encode_base64"):
        assert marker in rust, marker
    lib = (ROOT / "apps" / "windows" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "bridge::read_deliverable" in lib
    node = (ROOT / "apps" / "windows" / "src-tauri" / "src" / "node.rs").read_text(encoding="utf-8")
    for marker in ("merged_config", "plugin.call@1", "allow_plugin_call",
                   "plugin_policy_configured", "现有 Node 配置无法解析，未覆盖"):
        assert marker in node, marker
