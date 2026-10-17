import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nexus.core.app import create_app
from nexus.core.config import CoreConfig
from nexus.node.service_config import NodeServiceConfig
from nexus.service_config import load_config


def core(tmp_path, **changes):
    values = dict(data_dir=tmp_path, owner_id=uuid4(), local_http=True,
                  origins=frozenset({"http://127.0.0.1:8000"}))
    values.update(changes)
    return CoreConfig(**values)


@pytest.mark.parametrize("changes", [
    {"data_dir": Path("relative")}, {"host": "0.0.0.0"}, {"host": "localhost"},
    {"local_http": False}, {"port": True}, {"port": 0}, {"port": 65536},
    {"origins": frozenset({"http://192.168.0.1:8000"})},
    {"heartbeat_interval_seconds": 10, "heartbeat_timeout_seconds": 10},
    {"max_message_bytes": 2048, "max_result_bytes": 128},
    {"max_result_bytes": 32769}, {"execution_timeout_seconds": 11},
    {"tls_cert": Path("certificate.pem")},
])
def test_core_startup_config_rejects_unsafe_or_unbounded_values(tmp_path, changes):
    with pytest.raises(ValidationError):
        core(tmp_path, **changes)


@pytest.mark.parametrize("url,local", [
    ("ws://192.168.1.2/ws/v1/node", True), ("ws://localhost/ws/v1/node", True),
    ("ws://127.0.0.1/ws/v1/node", False), ("https://core.test/ws/v1/node", False),
    ("wss://secret@core.test/ws/v1/node", False),
    ("wss://core.test/ws/v1/node?token=secret", False),
    ("wss://core.test/ws/v1/node#secret", False), ("wss://core.test/other", False),
    ("ws://127.0.0.1:0/ws/v1/node", True), ("wss://core.test:invalid/ws/v1/node", False),
])
def test_node_url_requires_fixed_authenticated_endpoint(tmp_path, url, local):
    with pytest.raises(ValidationError):
        NodeServiceConfig(node_id=uuid4(), data_dir=tmp_path, core_url=url, local_http=local)


def test_secure_defaults_and_zero_configuration_side_effects(tmp_path):
    new_path = tmp_path / "not-created"
    cfg = core(new_path, local_http=False, origins=frozenset({"https://core.test"}),
               tls_cert=tmp_path / "cert.pem", tls_key=tmp_path / "key.pem")
    assert not new_path.exists()
    assert not cfg.nodes
    node = NodeServiceConfig(node_id=uuid4(), data_dir=tmp_path, core_url="wss://core.test/ws/v1/node")
    assert not node.local_http and not node.allowed_tools


def test_toml_environment_priority_strictness_and_no_secrets(tmp_path, monkeypatch):
    file = tmp_path / "core.toml"
    file.write_text(f'data_dir = {json.dumps(str(tmp_path))}\nowner_id = "{uuid4()}"\nlocal_http = true\norigins = ["http://127.0.0.1:8000"]\nport=8000\n', encoding="utf-8")
    monkeypatch.setenv("NEXUS_PORT", "8123")
    cfg = load_config(file, CoreConfig, {"NEXUS_PORT": "port"})
    assert cfg.port == 8123
    file.write_text(file.read_text() + '\ntoken="must-not-be-accepted"\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(file, CoreConfig, {})
    file.write_text(" " * 65537)
    with pytest.raises(ValueError, match="64 KiB"):
        load_config(file, CoreConfig, {})


def test_core_data_directory_single_owner_and_reopen(tmp_path):
    cfg = core(tmp_path)
    with TestClient(create_app(cfg)):
        with pytest.raises(OSError):
            with TestClient(create_app(cfg)):
                pytest.fail("second Core acquired the same data directory")
    with TestClient(create_app(cfg)):
        pass
