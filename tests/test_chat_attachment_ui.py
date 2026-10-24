"""Attachment UI assets stay local, bounded and separate from model consent."""

from pathlib import Path

from fastapi.testclient import TestClient

from nexus.core.app import create_app
from test_node_link import ORIGIN, core_config

ROOT = Path(__file__).resolve().parents[1]


def test_app_attachment_composer_assets_are_served_without_accept_filter(world):
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        page = client.get("/app")
        assert page.status_code == 200
        html = page.text
        for marker in ['id="attachment-input"', 'id="chat-attach"', 'id="dock-attach"',
                       'id="chat-attachments"', 'id="dock-attachments"', 'multiple']:
            assert marker in html, marker
        assert "accept=" not in html
        assert "app.css?v=20261023a" in html and "main.js?v=20261023a" in html

        script = client.get("/static/app/attachments.js")
        assert script.status_code == 200
        source = script.text
        for marker in ["model-preview", "attachment_ids", "attachments_digest",
                       "allow_attachment_model", "encoding=base64", "ATTACHMENT_IN_USE",
                       "UPLOAD_TOO_LARGE", "attachments_digest", "clearAttachments",
                       "图片输入未启用：本轮仅发送文件信息"]:
            assert marker in source, marker
        assert "accept=" not in source

        images = client.get("/static/app/image.js")
        assert images.status_code == 200
        assert "inspectAttachmentSnapshot" in images.text
        assert "createAttachmentEvidence" in images.text

        css = client.get("/static/app/app.css")
        assert ".attachment-progress" in css.text and "drop-active" in css.text
        assert ".dock-attachments" in css.text


def test_legacy_console_exposes_attachment_selection_and_consent():
    html = (ROOT / "src" / "nexus" / "web" / "index.html").read_text(encoding="utf-8")
    for marker in ['id="chat-attachments"', 'id="chat-attach"', 'id="chat-files"',
                   'id="chat-attachment-consent"', "/static/chat-attachments.js"]:
        assert marker in html, marker
    assert "accept=" not in html
    source = (ROOT / "src" / "nexus" / "web" / "chat-attachments.js").read_text(encoding="utf-8")
    for marker in ["model-preview", "attachment_ids", "allow_attachment_model",
                   "ATTACHMENT_IN_USE", "chatAttachmentSent", "encoding=base64"]:
        # The legacy browser path uploads raw bytes; Base64 belongs to the
        # Windows bridge. The model contract markers still apply here.
        if marker == "encoding=base64":
            continue
        assert marker in source, marker
    assert "required" not in html.split('id="chat-text"')[1].split(">")[0]


def test_windows_ui_and_bridge_keep_attachment_transfer_bounded():
    ui = ROOT / "apps" / "windows" / "ui"
    html = (ui / "app.html").read_text(encoding="utf-8")
    assert 'id="attachment-input"' in html and 'id="chat-attach"' in html
    assert "accept=" not in html
    source = (ui / "attachments.js").read_text(encoding="utf-8")
    for marker in ["attachmentRequest", "cancelAttachment", "encoding=base64"]:
        assert marker in source, marker
    bridge = ROOT / "apps" / "windows" / "src-tauri" / "src" / "bridge.rs"
    text = bridge.read_text(encoding="utf-8")
    assert "attachment_request" in text and "cancel_attachment_request" in text
    assert "MAX_ATTACHMENT_BASE64" in text and "23 * 1024 * 1024" in text
    assert "response.bytes()" not in text
    lib = ROOT / "apps" / "windows" / "src-tauri" / "src" / "lib.rs"
    assert "bridge::attachment_request" in lib.read_text(encoding="utf-8")
