import json
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from nexus.contracts.errors import ErrorCode, ProtocolError
from nexus.contracts.task import ListDirectoryArguments
from nexus.node import process as process_module
from nexus.node.runtime import NodeRuntime
from node_support import blocking_worker, command, configuration, sign


def wait_file(path, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return int(path.read_text())
        time.sleep(0.02)
    pytest.fail("controlled worker did not start")


def inject_blocker(monkeypatch, marker):
    original = subprocess.Popen
    def spawn(args, **kwargs):
        assert args[-1] == "nexus.node.worker"
        return original([sys.executable, "-B", "-c", blocking_worker(marker)], **kwargs)
    monkeypatch.setattr(process_module.subprocess, "Popen", spawn)


def test_real_windows_tools_cache_and_independent_restart(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "original").touch()
    cfg = configuration(tmp_path, roots={"workspace": root})
    item = command(cfg, directory=True)
    with NodeRuntime(cfg) as runtime:
        status = runtime.execute(item)
        assert status.phase == "completed" and status.result.outcome == "succeeded"
        assert [e.name for e in status.result.result.entries] == ["original"]
        (root / "new").touch()
        assert runtime.execute(item) == status
        system = runtime.execute(command(cfg))
        assert system.result.result.os_name == "Windows"
    # Reopen in a genuinely separate Python process, forbid any worker spawn.
    script = '''
import json, sys
from pydantic import TypeAdapter
from nexus.node.config import NodeConfig
from nexus.node.runtime import NodeRuntime
from nexus.contracts.messages import CommandExecute
data=json.load(sys.stdin)
with NodeRuntime(NodeConfig.model_validate_json(json.dumps(data["config"]))) as runtime:
    def forbidden(*args, **kwargs):
        raise AssertionError("cached attempt executed twice")
    runtime._worker.run=forbidden
    command=TypeAdapter(CommandExecute).validate_json(json.dumps(data["command"]))
    print(runtime.execute(command).model_dump_json())
'''
    result = subprocess.run([sys.executable, "-B", "-c", script], input=json.dumps({
        "config": cfg.model_dump(mode="json"), "command": item.model_dump(mode="json"),
    }), text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == status.model_dump(mode="json")


def test_new_session_replays_expired_cached_result(tmp_path):
    cfg = configuration(tmp_path)
    item = command(cfg, expires_at=datetime.now(UTC) + timedelta(seconds=2))
    with NodeRuntime(cfg) as runtime:
        first = runtime.execute(item)
        assert first.phase == "completed"
    time.sleep(2)
    with NodeRuntime(cfg) as runtime:
        new_session = uuid4()
        replay = runtime.execute(item.model_copy(update={"session_id": new_session}))
        assert replay.result.observed_at == first.result.observed_at
        assert replay.session_id == replay.result.session_id == new_session


@pytest.mark.parametrize("mode", ["disabled", "missing_root", "expired", "digest", "parameters"])
def test_invalid_commands_never_start_a_worker(tmp_path, monkeypatch, mode):
    cfg = configuration(tmp_path, allowed_tools=frozenset() if mode == "disabled" else frozenset({"system.info@1", "filesystem.list@1"}))
    item = command(cfg, directory=mode == "missing_root")
    if mode == "expired":
        item = sign(item.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}), cfg.node_id)
    elif mode == "digest":
        item = item.model_copy(update={"command_digest": "0" * 64})
    elif mode == "parameters":
        item = item.model_copy(update={"arguments": {"path": "C:/Windows"}})
    with NodeRuntime(cfg) as runtime:
        monkeypatch.setattr(runtime._worker, "run", lambda *args: pytest.fail("forbidden tool started"))
        with pytest.raises((ProtocolError, ValueError)):
            runtime.execute(item)
        assert runtime.receipts.get(item.attempt_id) is None


def test_same_attempt_changed_arguments_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    cfg = configuration(tmp_path, roots={"workspace": root})
    item = command(cfg, directory=True)
    with NodeRuntime(cfg) as runtime:
        runtime.execute(item)
        changed = sign(item.model_copy(update={"arguments": ListDirectoryArguments(root_id="workspace", limit=1)}), cfg.node_id)
        with pytest.raises(ProtocolError) as exc:
            runtime.execute(changed)
        assert exc.value.error.code == ErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.parametrize("stage", ["accept", "start", "finish"])
def test_receipt_failure_stops_execution_or_result_publication(tmp_path, monkeypatch, stage):
    cfg = configuration(tmp_path)
    item = command(cfg)
    with NodeRuntime(cfg) as runtime:
        method = {"accept": "INSERT", "start": "UPDATE", "finish": "UPDATE"}[stage]
        condition = {"accept": "", "start": "WHEN NEW.phase='started'", "finish": "WHEN NEW.phase='completed'"}[stage]
        with runtime.receipts.transaction() as conn:
            conn.execute(f"CREATE TRIGGER failure BEFORE {method} ON execution_receipts {condition} BEGIN SELECT RAISE(ABORT, 'injected'); END")
        if stage != "finish":
            monkeypatch.setattr(runtime._worker, "run", lambda *args: pytest.fail("execution without durable receipt"))
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            runtime.execute(item)
        receipt = runtime.receipts.get(item.attempt_id)
        assert (receipt is None) if stage == "accept" else receipt.phase == ("received" if stage == "start" else "started")
        with pytest.raises(ProtocolError) as exc:
            runtime.execute(command(cfg))
        assert exc.value.error.code == ErrorCode.BUSY
    if stage == "finish":
        with NodeRuntime(cfg) as restarted:
            assert restarted.execute(item).phase == "unknown"


def test_local_timeout_kills_actual_blocking_worker(tmp_path, monkeypatch):
    cfg = configuration(tmp_path, execution_timeout_seconds=2)
    marker = tmp_path / "worker.pid"
    inject_blocker(monkeypatch, marker)
    with NodeRuntime(cfg) as runtime:
        status = runtime.execute(command(cfg))
        pid = wait_file(marker)
        assert status.phase == "completed"
        assert status.result.error.code == ErrorCode.TOOL_TIMEOUT
        assert_process_dead(pid)


def test_concurrency_dedup_and_cancel_confirms_real_stop(tmp_path, monkeypatch):
    cfg = configuration(tmp_path)
    item = command(cfg)
    marker = tmp_path / "worker.pid"
    inject_blocker(monkeypatch, marker)
    with NodeRuntime(cfg) as runtime, ThreadPoolExecutor(max_workers=1) as executor:
        work = executor.submit(runtime.execute, item)
        pid = wait_file(marker)
        assert runtime.execute(item).phase == "started"
        assert runtime.status(item.attempt_id, item.session_id).phase == "started"
        with pytest.raises(ProtocolError) as exc:
            runtime.execute(command(cfg))
        assert exc.value.error.code == ErrorCode.BUSY
        assert runtime.cancel(item.attempt_id) is True
        assert work.result(timeout=5).phase == "cancelled"
        assert runtime.cancel(item.attempt_id) is False
        assert_process_dead(pid)


def assert_process_dead(pid):
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x100000, False, pid)
    if handle:
        try:
            assert api.WaitForSingleObject(handle, 3000) == 0, "owned tool process survived stop"
        finally:
            api.CloseHandle(handle)
    else:
        assert ctypes.get_last_error() == 87  # PID no longer exists.


def test_unknown_started_receipt_never_runs_again_and_received_can_resume(tmp_path, monkeypatch):
    cfg = configuration(tmp_path)
    started, received = command(cfg), command(cfg)
    with NodeRuntime(cfg) as runtime:
        runtime.receipts.accept(started, runtime.boot_id)
        runtime.receipts.start(started.attempt_id, f"Local\\NEXUS-{uuid4().hex}")
        runtime.receipts.accept(received, runtime.boot_id)
    with NodeRuntime(cfg) as restarted:
        monkeypatch.setattr(restarted._worker, "run", lambda *args: pytest.fail("unknown attempt was rerun"))
        assert restarted.execute(started).phase == "unknown"
        assert restarted.receipts.get(started.attempt_id).execution_stopped
    with NodeRuntime(cfg) as restarted:
        assert restarted.execute(received).phase == "completed"


def test_node_data_lock_and_closed_runtime(tmp_path):
    cfg = configuration(tmp_path)
    with NodeRuntime(cfg) as runtime:
        with pytest.raises(OSError):
            NodeRuntime(cfg)
    with pytest.raises(ProtocolError) as exc:
        runtime.execute(command(cfg))
    assert exc.value.error.code == ErrorCode.BUSY


def test_assignment_failure_never_opens_tool_gate(tmp_path, monkeypatch):
    from nexus.node.windows import WindowsJob
    cfg = configuration(tmp_path)
    marker = tmp_path / "must-not-exist"
    inject_blocker(monkeypatch, marker)
    def deny(*_):
        raise OSError("injected Job assignment failure")
    monkeypatch.setattr(WindowsJob, "assign", deny)
    with NodeRuntime(cfg) as runtime:
        item = command(cfg)
        assert runtime.execute(item).phase == "unknown"
        assert runtime.receipts.get(item.attempt_id).execution_stopped
    assert not marker.exists()


def test_unconfirmed_old_job_blocks_new_execution(tmp_path, monkeypatch):
    import nexus.node.runtime as runtime_module
    cfg = configuration(tmp_path)
    item = command(cfg)
    with NodeRuntime(cfg) as runtime:
        runtime.receipts.accept(item, runtime.boot_id)
        runtime.receipts.start(item.attempt_id, "injected-unqueryable-job")
    def cannot_query(_):
        raise OSError("injected query denial")
    monkeypatch.setattr(runtime_module, "job_stopped", cannot_query)
    with NodeRuntime(cfg) as restarted:
        assert restarted.execute(item).phase == "unknown"
        monkeypatch.setattr(restarted._worker, "run", lambda *args: pytest.fail("unconfirmed previous worker"))
        with pytest.raises(ProtocolError) as exc:
            restarted.execute(command(cfg))
        assert exc.value.error.code == ErrorCode.EXECUTION_UNKNOWN


def test_parent_death_kills_job_worker_and_restart_stays_unknown(tmp_path):
    import ctypes
    from ctypes import wintypes
    cfg = configuration(tmp_path)
    item = command(cfg)
    marker = tmp_path / "child.pid"
    script = '''
import json, os, subprocess, sys
from nexus.node import process as process_module
from nexus.node.config import NodeConfig
from nexus.node.runtime import NodeRuntime, COMMAND
data=json.load(sys.stdin)
original=subprocess.Popen
def spawn(args, **kwargs):
    return original([sys.executable, "-B", "-c", data["worker"]], **kwargs)
process_module.subprocess.Popen=spawn
with NodeRuntime(NodeConfig.model_validate_json(json.dumps(data["config"]))) as runtime:
    print(os.getpid(), flush=True)
    runtime.execute(COMMAND.validate_json(json.dumps(data["command"])))
'''
    parent = subprocess.Popen([sys.executable, "-B", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    try:
        parent.stdin.write(json.dumps({"config": cfg.model_dump(mode="json"), "command": item.model_dump(mode="json"), "worker": blocking_worker(marker)}).encode())
        parent.stdin.close()
        parent.stdin = None
        node_pid = int(parent.stdout.readline())
        handle = api.OpenProcess(0x100001, False, node_pid)  # Owned Node only: TERMINATE | SYNCHRONIZE.
        assert handle
        worker_pid = wait_file(marker)
        assert api.TerminateProcess(handle, 1)
        parent.communicate(timeout=5)
        assert_process_dead(worker_pid)
        with NodeRuntime(cfg) as restarted:
            restarted._worker.run = lambda *args: pytest.fail("crashed started attempt must never rerun")
            status = restarted.execute(item)
            assert status.phase == "unknown" and status.result is None
            assert restarted.receipts.get(item.attempt_id).execution_stopped
    finally:
        if handle:
            api.TerminateProcess(handle, 1)
            api.CloseHandle(handle)
        if parent.poll() is None:
            parent.kill()
        parent.communicate(timeout=5)


def test_ungated_worker_exits_without_tool_execution(tmp_path):
    marker = tmp_path / "must-not-exist"
    completed = subprocess.run([sys.executable, "-B", "-c", blocking_worker(marker)], input=b"", capture_output=True, timeout=5)
    assert completed.returncode == 2
    assert not marker.exists() and completed.stdout == b""
