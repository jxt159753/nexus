"""Chat-first same-origin client shell is served by Core and keeps policy headers."""

from fastapi.testclient import TestClient

from nexus.core.app import create_app
from test_node_link import ORIGIN, core_config


def test_chat_first_shell_and_assets_are_same_origin(world):
    with TestClient(create_app(core_config(world)), base_url=ORIGIN) as client:
        page = client.get("/app")
        assert page.status_code == 200
        assert "NEXUS · 对话" in page.text
        assert 'id="dock-form"' in page.text
        assert 'id="chat-view"' in page.text
        assert 'id="visual-canvas"' in page.text
        assert "有什么可以帮你？" not in page.text
        assert "NEXUS · AGENT PROCESS" not in page.text
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert "img-src 'self' data:" in page.headers["content-security-policy"]
        assert "'sha256-UaO1DKdOZTH3g72w3U/IhBu3y5cxME0JyJYosMMMerQ='" in page.headers["content-security-policy"]
        assert "'sha256-jH4JDM+p01kuu6ou/A4MgTq6B9+X7dUZGHEdH52L4GY='" in page.headers["content-security-policy"]
        assert "/static/app/vendor/three.module.js" in page.text
        assert page.headers["cache-control"] == "no-store"

        css = client.get("/static/app/app.css")
        assert css.status_code == 200
        assert "text/css" in css.headers["content-type"]

        script = client.get("/static/app/main.js")
        assert script.status_code == 200
        assert "javascript" in script.headers["content-type"]
        assert "from \"./api.js\"" in script.text

        visualization = client.get("/static/app/visualization.js")
        assert visualization.status_code == 200
        assert 'from "three"' in visualization.text
        three = client.get("/static/app/vendor/three.module.js")
        assert three.status_code == 200
        assert "javascript" in three.headers["content-type"]
        composer = client.get("/static/app/vendor/addons/postprocessing/EffectComposer.js")
        assert composer.status_code == 200
        assert "three/addons/" in page.text
        markdown = client.get("/static/app/markdown.js")
        assert markdown.status_code == 200
        assert "marked.esm.js" in markdown.text
        marked = client.get("/static/app/vendor/markdown/marked.esm.js")
        assert marked.status_code == 200
        purify = client.get("/static/app/vendor/markdown/purify.es.mjs")
        assert purify.status_code == 200
        assert "javascript" in purify.headers["content-type"]
        image = client.get("/static/app/image.js")
        assert image.status_code == 200
        assert "inspectImageResult" in image.text
