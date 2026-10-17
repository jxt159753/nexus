
import pytest

from nexus.settings import load_settings


def test_missing_data_dir_fails(monkeypatch):
    monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)

    with pytest.raises(ValueError, match="NEXUS_DATA_DIR is required"):
        load_settings()


def test_valid_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_HOST", "127.0.0.1")
    monkeypatch.setenv("NEXUS_PORT", "8000")

    settings = load_settings()

    assert settings.data_dir == tmp_path
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000


def test_remote_binding_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_HOST", "0.0.0.0")

    with pytest.raises(ValueError, match="localhost"):
        load_settings()


@pytest.mark.parametrize("port", ["abc", "0", "65536"])
def test_invalid_port_is_rejected(monkeypatch, tmp_path, port):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_HOST", "127.0.0.1")
    monkeypatch.setenv("NEXUS_PORT", port)

    with pytest.raises(ValueError, match="NEXUS_PORT"):
        load_settings()
