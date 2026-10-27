"""Publication checks distinguish secrets and real endpoints from examples."""

import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "public_check", Path(__file__).resolve().parents[1] / "scripts/check-public-repo.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_sensitive_values_are_detected_without_being_returned():
    secret = b"sk-" + b"a" * 32
    result = module.findings("config/example.toml", b"key = '" + secret + b"'\n")
    assert ("provider-token", 1) in result
    assert secret.decode() not in repr(result)
    key = b"-----BEGIN " + b"PRIVATE KEY-----"
    assert ("private-key", 2) in module.findings("source.py", b"# key\n" + key)


def test_examples_are_allowed_but_public_ip_and_private_files_are_reported():
    assert module.findings(".env.example", b"KEY=YOUR_KEY") == []
    assert module.findings("tests/fixture.py", b'API_KEY="synthetic-windows-attachment-only"') == []
    assert module.findings("docs/setup.md", b"http://127.0.0.1:8000 https://192.0.2.1/") == []
    address = b"https://" + b"8.8.8.8/"
    assert ("public-ip-url", 1) in module.findings("docs/setup.md", address)
    assert module.findings("config/local.key", b"\0") == [("private-or-generated-file", 1)]
    assert module.findings("apps/ui/app.js.orig", b"old") == [("private-or-generated-file", 1)]
    assert module.findings("config/core.local.toml", b"owner_id='local'") == [("private-or-generated-file", 1)]
