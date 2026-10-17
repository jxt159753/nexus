import subprocess
import sys
import threading
from uuid import uuid4

import pytest

from nexus.contracts.base import MAX_MESSAGE_BYTES
from nexus.node import process as process_module
from nexus.node.process import ExecutionStopped, ManagedWorker
from nexus.node.windows import WindowsJob
from nexus.node.worker import WorkerRequest
from node_support import command, configuration


@pytest.mark.parametrize("source", [
    "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'x'*65537); sys.stdout.flush()",
    "import sys; sys.stdin.buffer.read(); raise SystemExit(3)",
])
def test_bounded_worker_output_and_crash(tmp_path, monkeypatch, source):
    original = subprocess.Popen
    monkeypatch.setattr(process_module.subprocess, "Popen", lambda args, **kwargs: original([sys.executable, "-B", "-c", source], **kwargs))
    with pytest.raises(ValueError):
        ManagedWorker().run(b"gate", f"Local\\NEXUS-{uuid4().hex}", 3, threading.Event())


def test_stuck_input_pipe_is_bounded_by_execution_deadline(tmp_path, monkeypatch):
    original = subprocess.Popen
    monkeypatch.setattr(process_module.subprocess, "Popen", lambda args, **kwargs: original([sys.executable, "-B", "-c", "import time; time.sleep(60)"], **kwargs))
    with pytest.raises(ExecutionStopped) as exc:
        ManagedWorker().run(b"x" * MAX_MESSAGE_BYTES, f"Local\\NEXUS-{uuid4().hex}", 1, threading.Event())
    assert exc.value.reason == "timeout"


def test_worker_itself_requires_exact_job_membership(tmp_path):
    cfg = configuration(tmp_path)
    name = f"Local\\NEXUS-{uuid4().hex}"
    job = WindowsJob(name)
    try:
        raw = WorkerRequest(command=command(cfg), config=cfg, job_name=name).model_dump_json().encode()
        result = subprocess.run([sys.executable, "-B", "-m", "nexus.node.worker"], input=raw, capture_output=True, timeout=5)
        assert result.returncode == 2
        assert result.stdout == b""
    finally:
        job.close()


def test_oversize_input_rejected_before_process_creation(monkeypatch):
    monkeypatch.setattr(process_module.subprocess, "Popen", lambda *a, **k: pytest.fail("oversize input spawned worker"))
    with pytest.raises(ValueError):
        ManagedWorker().run(b"x" * (MAX_MESSAGE_BYTES + 1), "unused", 1, threading.Event())
