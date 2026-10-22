"""One bounded worker, gated behind successful Windows Job assignment."""

import subprocess
import sys
import threading
import time

from nexus.contracts.base import MAX_MESSAGE_BYTES
from nexus.node.windows import WindowsJob, job_stopped


class ExecutionStopped(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ManagedWorker:
    def run(
        self, raw: bytes, job_name: str, timeout: float, cancel: threading.Event,
    ) -> bytes:
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("worker input exceeds limit")
        deadline = time.monotonic() + timeout
        job = WindowsJob(job_name)
        process = None
        reader = None
        writer = None
        output = bytearray()
        overflow = threading.Event()
        write_failed = threading.Event()
        try:
            args = [sys.executable, "--worker"] if getattr(sys, "frozen", False) else [sys.executable, "-B", "-m", "nexus.node.worker"]
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                shell=False, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            job.assign(process)

            def read_output() -> None:
                while True:
                    chunk = process.stdout.read(min(4096, MAX_MESSAGE_BYTES + 1 - len(output)))
                    if not chunk:
                        return
                    output.extend(chunk)
                    if len(output) > MAX_MESSAGE_BYTES:
                        overflow.set()
                        return

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            # Tool code cannot run until assignment above succeeds. Parent death
            # before assignment closes stdin; the worker sees EOF and exits.
            if cancel.is_set():
                raise ExecutionStopped("cancelled")
            def write_input() -> None:
                try:
                    process.stdin.write(raw)
                    process.stdin.close()
                except OSError:
                    write_failed.set()

            writer = threading.Thread(target=write_input, daemon=True)
            writer.start()
            while process.poll() is None:
                if cancel.is_set():
                    raise ExecutionStopped("cancelled")
                if time.monotonic() >= deadline:
                    raise ExecutionStopped("timeout")
                if overflow.is_set():
                    raise ValueError("worker output exceeds limit")
                if write_failed.is_set():
                    raise ValueError("worker input failed")
                time.sleep(0.01)
            if time.monotonic() >= deadline:
                raise ExecutionStopped("timeout")
            reader.join(timeout=2)
            if reader.is_alive() or overflow.is_set() or process.returncode != 0:
                raise ValueError("worker did not produce bounded evidence")
            return bytes(output)
        finally:
            # Closing the sole non-inheritable Job handle kills the whole tree.
            # Always wait for this owned process before reporting stopped.
            try:
                job.close()
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.kill()  # Also covers assignment failure, before the gate.
                    process.wait(timeout=3)
                    if writer is not None:
                        writer.join(timeout=2)
                    if process.stdin is not None:
                        process.stdin.close()
                    if reader is not None:
                        reader.join(timeout=2)
                    process.stdout.close()
                    stop_deadline = time.monotonic() + 3
                    while not job_stopped(job_name):
                        if time.monotonic() >= stop_deadline:
                            raise OSError("worker tree stop is unconfirmed")
                        time.sleep(0.01)
