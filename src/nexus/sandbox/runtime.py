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


class RunscRuntime:
    def __init__(self, config: SandboxConfig):
        self.config = config
        self.version = ""
        self._template: Path | None = None
        self._lock = threading.Lock()

    def preflight(self) -> None:
        if os.name == "nt":
            raise SandboxUnavailable("dedicated Linux Sandbox host required")
        if not self.config.runsc_path.is_file() or not os.access(self.config.runsc_path, os.X_OK):
            raise SandboxUnavailable("runsc is not installed or not executable")
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
        config = {
            "ociVersion": "1.0.2",
            "process": {
                "terminal": False,
                "user": {"uid": 0, "gid": 0, "additionalGids": []},
                "args": [self.config.python_path, "-I", "-B", "/work/tool.py"],
                "env": ["PATH=/usr/bin:/bin", "LANG=C.UTF-8"],
                "cwd": "/work",
                "capabilities": {"bounding": [], "effective": [], "inheritable": [], "permitted": [], "ambient": []},
                "noNewPrivileges": True,
                "rlimits": [{"type": "RLIMIT_NOFILE", "hard": 64, "soft": 64}],
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

    def execute(self, release_json: str, input_value: dict, timeout_seconds: int) -> dict:
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
            return {"outcome": "failed", "error_code": "SANDBOX_INPUT_INVALID"}
        work = Path(tempfile.mkdtemp(prefix="run-", dir=self.config.staging_dir))
        execution_id = uuid4().hex
        process = None
        stopped = False
        try:
            bundle = self._bundle(source, work)
            stdout_path = work / "stdout"
            stderr_path = work / "stderr"
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                process = subprocess.Popen(
                    [str(self.config.runsc_path), f"--root={self.config.runsc_root}", "--network=none",
                     "--platform=systrap", "run", f"--bundle={bundle}", f"--pid-file={work / 'pid'}", execution_id],
                    stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                    start_new_session=True, env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                )
                try:
                    process.communicate(encoded_input.encode("utf-8"), timeout=max(1, min(10, timeout_seconds)))
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                    stopped = self._cleanup(execution_id)
                    return {"outcome": "unknown" if not stopped else "failed",
                            "error_code": "SANDBOX_TIMEOUT", "stopped": stopped}
            size = stdout_path.stat().st_size
            if process.returncode != 0:
                return {"outcome": "failed", "error_code": "SANDBOX_EXIT", "stopped": True}
            if size == 0 or size > MAX_STDOUT or stderr_path.stat().st_size > MAX_STDERR:
                return {"outcome": "failed", "error_code": "SANDBOX_OUTPUT_LIMIT", "stopped": True}
            output = parse_strict_json(stdout_path.read_bytes())
            if type(output) is not dict:
                return {"outcome": "failed", "error_code": "SANDBOX_OUTPUT_INVALID", "stopped": True}
            return {"outcome": "succeeded", "output": output}
        except SandboxUnavailable as exc:
            return {"outcome": "failed", "error_code": "SANDBOX_UNAVAILABLE", "reason": str(exc)}
        except (OSError, ValueError, KeyError, TypeError):
            return {"outcome": "unknown", "error_code": "SANDBOX_EVIDENCE_UNKNOWN", "stopped": stopped}
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
            self._cleanup(execution_id)
            shutil.rmtree(work, ignore_errors=True)

    def _cleanup(self, execution_id: str) -> bool:
        try:
            result = subprocess.run(
                [str(self.config.runsc_path), f"--root={self.config.runsc_root}", "delete", "--force", execution_id],
                capture_output=True, timeout=5, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
