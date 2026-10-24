"""Fixed gVisor/runsc launcher. No shell, no host paths from candidates.

The launcher refuses to start unless runsc and the configured read-only image
content digest are verified. This module is intentionally unusable on Windows:
the work order forbids substituting an ordinary subprocess sandbox.
"""

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from nexus.contracts.extensions import canonical, parse_strict_json
from nexus.sandbox.config import SandboxConfig

MAX_STDOUT = 32 * 1024
MAX_STDERR = 8 * 1024
MAX_ARTIFACT = 64 * 1024


class SandboxUnavailable(RuntimeError):
    pass


def host_resource_limits() -> dict:
    """Check hard cgroup-v2 ancestors, outside the guest's synthetic /proc."""
    root = Path("/sys/fs/cgroup")
    memberships = Path("/proc/self/cgroup").read_text().splitlines()
    relative = next((line[3:] for line in memberships if line.startswith("0::")), None)
    if relative is None:
        raise SandboxUnavailable("host cgroup v2 resource envelope required")
    current = (root / relative.lstrip("/")).resolve()
    if not current.is_relative_to(root):
        raise SandboxUnavailable("invalid host cgroup membership")
    limits = {"memory": None, "pids": None, "cpu": None, "swap": None}
    for directory in (current, *current.parents):
        if not directory.is_relative_to(root):
            break
        for key, name in (("memory", "memory.max"), ("pids", "pids.max"), ("swap", "memory.swap.max")):
            path = directory / name
            if path.exists():
                value = path.read_text().strip()
                if value != "max":
                    limits[key] = min(limits[key], int(value)) if limits[key] is not None else int(value)
        path = directory / "cpu.max"
        if path.exists():
            quota, period = path.read_text().split()
            if quota != "max":
                ratio = int(quota) / int(period)
                limits["cpu"] = min(limits["cpu"], ratio) if limits["cpu"] is not None else ratio
    if (limits["memory"] is None or limits["memory"] > 512 * 1024 * 1024
            or limits["pids"] is None or limits["pids"] > 64
            or limits["cpu"] is None or limits["cpu"] > 1 or limits["swap"] != 0):
        raise SandboxUnavailable("host requires <=512 MiB, <=64 tasks, <=1 CPU and no swap")
    return limits


def tree_digest(root: Path, max_entries: int) -> str:
    if not root.is_dir():
        raise SandboxUnavailable("sandbox image rootfs is missing")
    hasher = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        count += 1
        if count > max_entries:
            raise SandboxUnavailable("sandbox image exceeds its entry limit")
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        hasher.update(relative.encode("utf-8") + b"\0")
        hasher.update(oct(stat.S_IMODE(info.st_mode)).encode() + b"\0")
        if stat.S_ISLNK(info.st_mode):
            hasher.update(b"link\0" + os.readlink(path).encode("utf-8") + b"\0")
        elif stat.S_ISREG(info.st_mode):
            hasher.update(b"file\0" + str(info.st_size).encode() + b"\0")
            with path.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
        elif stat.S_ISDIR(info.st_mode):
            hasher.update(b"dir\0")
        else:
            raise SandboxUnavailable("sandbox image contains a special file")
    return hasher.hexdigest()


def runtime_install_digest(binary: Path) -> str:
    """Bind the launcher and the split gVisor sentry components, when present."""
    with binary.open("rb") as source:
        launcher = hashlib.file_digest(source, "sha256").hexdigest()
    components = binary.parent / "gvisor-bin"
    value = {"runsc": launcher,
             "components": tree_digest(components, 4096) if components.is_dir() else None}
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class RunscRuntime:
    def __init__(self, config: SandboxConfig):
        self.config = config
        self.version = ""
        self._template: Path | None = None
        self._lock = threading.Lock()
        self._execution_lock = threading.Lock()
        self._blocked = False

    def preflight(self) -> None:
        if os.name == "nt":
            raise SandboxUnavailable("dedicated Linux Sandbox host required")
        mappings = Path("/proc/self/uid_map").read_text().splitlines()
        if os.geteuid() == 0 and any(int(line.split()[1]) == 0 for line in mappings):
            raise SandboxUnavailable("nonprivileged host user required")
        self.host_limits = host_resource_limits()
        if not self.config.runsc_path.is_file() or not os.access(self.config.runsc_path, os.X_OK):
            raise SandboxUnavailable("runsc is not installed or not executable")
        actual_runtime_digest = runtime_install_digest(self.config.runsc_path)
        if self.config.runsc_digest is None or actual_runtime_digest != self.config.runsc_digest:
            raise SandboxUnavailable("fixed runsc binary digest required")
        try:
            result = subprocess.run(
                [str(self.config.runsc_path), "--version"], capture_output=True, timeout=5,
                check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SandboxUnavailable("runsc version probe failed") from None
        version = (result.stdout + result.stderr).decode("utf-8", "replace").strip()[:200]
        if result.returncode != 0 or not version.lower().startswith("runsc"):
            raise SandboxUnavailable("runsc preflight rejected")
        digest = tree_digest(self.config.image_rootfs, self.config.max_image_entries)
        if digest != self.config.image_digest:
            raise SandboxUnavailable("sandbox image content digest mismatch")
        python = self.config.image_rootfs / self.config.python_path.lstrip("/")
        if not python.is_file() or not os.access(python, os.X_OK):
            raise SandboxUnavailable("fixed Python entrypoint is absent from the image")
        self.config.staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config.runsc_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.version = version

    def _rootfs_template(self) -> Path:
        with self._lock:
            if self._template is not None:
                return self._template
            template = self.config.staging_dir / f"rootfs-{self.config.image_digest[:16]}"
            if template.exists():
                if tree_digest(template, self.config.max_image_entries) != self.config.image_digest:
                    raise SandboxUnavailable("cached image digest mismatch")
                template.chmod(0o755)
                self._template = template
                return template
            building = self.config.staging_dir / f".rootfs-{uuid4().hex}"
            building.mkdir(mode=0o700)
            try:
                for source in sorted(self.config.image_rootfs.rglob("*")):
                    relative = source.relative_to(self.config.image_rootfs)
                    target = building / relative
                    info = source.lstat()
                    if stat.S_ISDIR(info.st_mode):
                        target.mkdir(mode=stat.S_IMODE(info.st_mode) | 0o700, exist_ok=True)
                    elif stat.S_ISLNK(info.st_mode):
                        target.symlink_to(os.readlink(source))
                    elif stat.S_ISREG(info.st_mode):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            os.link(source, target)
                        except OSError:
                            shutil.copy2(source, target)
                    else:
                        raise SandboxUnavailable("sandbox image contains a special file")
                building.chmod(0o755)
                building.rename(template)
            except BaseException:
                shutil.rmtree(building, ignore_errors=True)
                raise
            self._template = template
            return template

    def _bundle(self, source: str, work: Path) -> Path:
        bundle = work / "bundle"
        bundle.mkdir(mode=0o700)
        (work / "work").mkdir(mode=0o700)
        tool = work / "work" / "tool.py"
        tool.write_text(source, encoding="utf-8")
        tool.chmod(0o444)
        (work / "work").chmod(0o755)
        config = {
            "ociVersion": "1.0.2",
            "process": {
                "terminal": False,
                "user": {"uid": 65534, "gid": 65534, "additionalGids": []},
                "args": [self.config.python_path, "-I", "-B", "/work/tool.py"],
                "env": ["PATH=/usr/bin:/bin", "LANG=C.UTF-8"],
                "cwd": "/work",
                "capabilities": {"bounding": [], "effective": [], "inheritable": [], "permitted": [], "ambient": []},
                "noNewPrivileges": True,
                "rlimits": [{"type": "RLIMIT_NOFILE", "hard": 64, "soft": 64},
                            {"type": "RLIMIT_NPROC", "hard": 64, "soft": 64},
                            {"type": "RLIMIT_AS", "hard": 256 * 1024 * 1024, "soft": 256 * 1024 * 1024},
                            {"type": "RLIMIT_FSIZE", "hard": 16 * 1024 * 1024, "soft": 16 * 1024 * 1024}],
            },
            "root": {"path": str(self._rootfs_template()), "readonly": True},
            "hostname": "sandbox",
            "mounts": [
                {"destination": "/proc", "type": "proc", "source": "proc", "options": []},
                {"destination": "/work", "type": "bind", "source": str(work / "work"),
                 "options": ["rbind", "ro", "nosuid", "nodev", "noexec"]},
                {"destination": "/tmp", "type": "tmpfs", "source": "tmpfs",
                 "options": ["nosuid", "nodev", "noexec", "size=16m", "mode=1777"]},
            ],
            "linux": {
                "namespaces": [
                    {"type": "pid"}, {"type": "ipc"}, {"type": "uts"},
                    {"type": "mount"}, {"type": "network"}, {"type": "cgroup"},
                ],
                "resources": {
                    "memory": {"limit": 512 * 1024 * 1024, "swap": 0},
                    "pids": {"limit": 64},
                    "cpu": {"shares": 1024, "quota": 100000, "period": 100000},
                },
                "maskedPaths": ["/proc/acpi", "/proc/kcore", "/proc/keys", "/proc/sys", "/sys", "/dev"],
                "readonlyPaths": ["/proc/asound", "/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"],
            },
        }
        (bundle / "config.json").write_text(json.dumps(config, separators=(",", ":")), encoding="utf-8")
        return bundle

    def execute(self, release_json: str, input_value: dict, timeout_seconds: int, *,
                execution_id: str | None = None, cancel_event: threading.Event | None = None) -> dict:
        try:
            release = parse_strict_json(release_json)
            if canonical(release) != release_json:
                raise ValueError("release is not canonical")
            source = release["source"]
            if release.get("kind") != "tool" or not isinstance(source, str) or len(source.encode("utf-8")) > MAX_ARTIFACT:
                raise ValueError("not a bounded Tool release")
            encoded_input = json.dumps(input_value, ensure_ascii=False, separators=(",", ":"))
            if len(encoded_input.encode("utf-8")) > 16384:
                raise ValueError("input exceeds budget")
        except (ValueError, KeyError, TypeError):
            return {"outcome": "failed", "error_code": "SANDBOX_INPUT_INVALID", "stopped": True}
        if self._blocked or not self._execution_lock.acquire(blocking=False):
            return {"outcome": "failed", "error_code": "SANDBOX_UNAVAILABLE", "stopped": True}
        execution_id = uuid4().hex if execution_id is None else execution_id
        if len(execution_id) != 32 or any(c not in "0123456789abcdef" for c in execution_id):
            self._execution_lock.release()
            return {"outcome": "failed", "error_code": "SANDBOX_INPUT_INVALID", "stopped": True}
        work = None
        process = None
        stopped = False
        try:
            host_resource_limits()
            work = Path(tempfile.mkdtemp(prefix="run-", dir=self.config.staging_dir))
            bundle = self._bundle(source, work)
            process = subprocess.Popen(
                [str(self.config.runsc_path), f"--root={self.config.runsc_root}", "--network=none",
                 "--ignore-cgroups=true",
                 f"--platform={self.config.platform}", "run", f"--bundle={bundle}", f"--pid-file={work / 'pid'}", execution_id],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, start_new_session=True, env={"PATH": "/usr/bin:/bin", "LANG": "C", "GOMAXPROCS": "1"},
            )
            output, error, stopped = self._exchange(process, encoded_input.encode("utf-8"), timeout_seconds, cancel_event,
                                                    lambda: self.reconcile(execution_id))
            if not stopped:
                self._blocked = True
                return {"outcome": "unknown", "error_code": "SANDBOX_STOP_UNKNOWN", "stopped": False}
            if error:
                return {"outcome": "failed", "error_code": error, "stopped": True}
            if process.returncode != 0:
                return {"outcome": "failed", "error_code": "SANDBOX_EXIT", "stopped": True}
            output = parse_strict_json(output)
            if type(output) is not dict:
                return {"outcome": "failed", "error_code": "SANDBOX_OUTPUT_INVALID", "stopped": True}
            return {"outcome": "succeeded", "output": output}
        except SandboxUnavailable as exc:
            return {"outcome": "failed", "error_code": "SANDBOX_UNAVAILABLE", "reason": str(exc), "stopped": True}
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            return {"outcome": "failed" if stopped else "unknown",
                    "error_code": "SANDBOX_OUTPUT_INVALID" if stopped else "SANDBOX_EVIDENCE_UNKNOWN", "stopped": stopped}
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
            if not stopped and not self.reconcile(execution_id):
                self._blocked = True
            if work is not None:
                try:
                    shutil.rmtree(work)
                except OSError:
                    self._blocked = True
            self._execution_lock.release()

    @staticmethod
    def _exchange(process, incoming: bytes, timeout_seconds: int, cancel_event, cleanup) -> tuple[bytes, str | None, bool]:
        """Bound capture while producing output; never spool candidate output to disk."""
        buffers = [bytearray(), bytearray()]
        overflow = threading.Event()

        def read(pipe, target, limit):
            try:
                while chunk := pipe.read(4096):
                    remaining = limit - len(target)
                    target.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        overflow.set()
                        return
            except (OSError, ValueError):
                overflow.set()

        def write():
            try:
                view = memoryview(incoming)
                while view:
                    written = process.stdin.write(view)
                    if not written:
                        break
                    view = view[written:]
                process.stdin.close()
            except (OSError, ValueError):
                pass

        threads = [threading.Thread(target=read, args=(process.stdout, buffers[0], MAX_STDOUT), daemon=True),
                   threading.Thread(target=read, args=(process.stderr, buffers[1], MAX_STDERR), daemon=True),
                   threading.Thread(target=write, daemon=True)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + max(1, min(10, timeout_seconds))
        error = None
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                error = "SANDBOX_CANCELLED"
            elif overflow.is_set():
                error = "SANDBOX_OUTPUT_LIMIT"
            elif time.monotonic() >= deadline:
                error = "SANDBOX_TIMEOUT"
            if error:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
                break
            time.sleep(.02)
        stopped = cleanup()
        for thread in threads:
            thread.join(timeout=1)
        if any(thread.is_alive() for thread in threads):
            error = error or "SANDBOX_EVIDENCE_UNKNOWN"
        if overflow.is_set():
            error = error or "SANDBOX_OUTPUT_LIMIT"
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
        return bytes(buffers[0]), error, stopped

    def _cleanup(self, execution_id: str) -> bool:
        try:
            result = subprocess.run(
                [str(self.config.runsc_path), f"--root={self.config.runsc_root}", "delete", "--force", execution_id],
                capture_output=True, timeout=5, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def reconcile(self, execution_id: str) -> bool:
        """Remove only this receipt's container, then confirm its identity is absent."""
        if len(execution_id) != 32 or any(c not in "0123456789abcdef" for c in execution_id):
            return False
        self._cleanup(execution_id)
        try:
            result = subprocess.run([str(self.config.runsc_path), f"--root={self.config.runsc_root}", "list", "--format=json"],
                capture_output=True, timeout=5, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C"})
            if result.returncode or len(result.stdout) > 65536:
                return False
            containers = json.loads(result.stdout)
            if containers is None:
                containers = []
            if not isinstance(containers, list):
                return False
            return all(item["id"] != execution_id for item in containers)
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            return False
