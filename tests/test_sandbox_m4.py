"""v2 Sandbox protocol and refusal-path checks. No runsc execution is claimed."""

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from nexus.contracts.errors import ProtocolError
from nexus.contracts.sandbox import (
    SandboxExecute,
    SandboxHello,
    SandboxResult,
    encode_sandbox_message,
    parse_sandbox_message,
)
from nexus.sandbox.config import SandboxConfig
from nexus.sandbox.runtime import RunscRuntime, SandboxUnavailable, tree_digest


def hello(**changes):
    value = dict(node_id=uuid4(), node_version="test", boot_id=uuid4(), runtime="runsc",
                 runtime_version="runsc version test", image_digest="a" * 64,
                 available=True, reason=None, verification_profiles=["directory_entry_stats_v1"])
    value.update(changes)
    return SandboxHello(**value)


def test_v2_protocol_rejects_v1_unknown_fields_and_unknown_versions():
    message = encode_sandbox_message("sandbox.hello", hello())
    assert parse_sandbox_message(message).type == "sandbox.hello"
    with pytest.raises(ProtocolError):
        parse_sandbox_message('{"protocol_version":1,"type":"node.hello","message_id":"%s","correlation_id":null,"sent_at":"2026-10-19T00:00:00Z","payload":{}}' % uuid4())
    with pytest.raises(ProtocolError):
        parse_sandbox_message('{"protocol_version":2,"type":"sandbox.execute","message_id":"%s","correlation_id":null,"sent_at":"2026-10-19T00:00:00Z","payload":{"session_id":"%s"}}' % (uuid4(), uuid4()))
    with pytest.raises(ProtocolError):
        parse_sandbox_message('{"protocol_version":2,"type":"sandbox.hello","message_id":"%s","correlation_id":null,"sent_at":"2026-10-19T00:00:00Z","payload":{"node_id":"%s"}}' % (uuid4(), uuid4()))


def test_result_requires_exclusive_output_or_error():
    common = dict(session_id=uuid4(), execution_id=uuid4(), artifact_digest="a" * 64)
    assert SandboxResult(**common, outcome="succeeded", output_json='{"counts":{}}').outcome == "succeeded"
    with pytest.raises(ValueError):
        SandboxResult(**common, outcome="succeeded")
    with pytest.raises(ValueError):
        SandboxResult(**common, outcome="failed")
    with pytest.raises(ValueError):
        SandboxResult(**common, outcome="unknown", error_code="X", stopped=True)


def test_sandbox_config_never_accepts_a_windows_host(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows refusal path")
    with pytest.raises(ValueError):
        SandboxConfig(node_id=uuid4(), core_url="wss://example.invalid/ws/v2/sandbox",
                      data_dir=tmp_path / "data", staging_dir=tmp_path / "stage",
                      runsc_path=Path("C:/runsc.exe"), runsc_root=tmp_path / "state",
                      image_rootfs=tmp_path / "rootfs", image_digest="a" * 64)


def test_runsc_preflight_refuses_on_windows_before_any_candidate_runs(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows refusal path")
    runtime = RunscRuntime(SimpleNamespace(runsc_path=tmp_path / "missing", image_rootfs=tmp_path,
                                           max_image_entries=1000, config_root=tmp_path,
                                           staging_dir=tmp_path, runsc_root=tmp_path))
    with pytest.raises(SandboxUnavailable):
        runtime.preflight()


def test_tree_digest_is_content_bound(tmp_path):
    root = tmp_path / "rootfs"
    (root / "usr").mkdir(parents=True)
    (root / "usr" / "python").write_text("one", encoding="utf-8")
    first = tree_digest(root, 100)
    assert len(first) == 64
    (root / "usr" / "python").write_text("two", encoding="utf-8")
    assert tree_digest(root, 100) != first
    (root / "usr" / "extra").write_text("three", encoding="utf-8")
    assert tree_digest(root, 100) != first
