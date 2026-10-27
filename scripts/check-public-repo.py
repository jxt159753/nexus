"""Read-only publication check; reports locations, never matched secret values.

Checks Git-visible working files by default, including untracked source. --history
also checks every reachable local Git blob. This is a targeted hygiene check,
not a comprehensive secret scanner or a review of images/remote PR artifacts.
"""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path
import re
import subprocess
from collections.abc import Iterator


ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "provider-token": re.compile(
        rb"\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}"
        rb"|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16})\b"
    ),
    "credential-literal": re.compile(
        rb'''(?i)(?:api[_-]?key|access[_-]?token|owner[_-]?token|node[_-]?token|password|secret)\s*[=:]\s*["'][A-Za-z0-9_+/=-]{32,}["']'''
    ),
}
IP_URL = re.compile(rb"(?:https?|wss?)://(\d{1,3}(?:\.\d{1,3}){3})(?=[:/\s\"')]|$)")
PERSONAL_PATH = re.compile(rb"(?i)[A-Z]:[\\/]+Users[\\/]+([A-Za-z0-9_.-]+)")
PRIVATE_FILE = re.compile(
    r"(?i)(?:\.(?:pem|key|p12|pfx|jks|keystore|sqlite3?|db)(?:-wal|-shm|-journal)?$"
    r"|(?:^|/)\.env(?:\.[^/]+)?$|\.(?:orig|rej)$|(?:^|/)local\.properties$"
    r"|(?:^|/)config/[^/]+\.(?:local|private)\.toml$"
    r"|(?:^|/)nul$|(?:^|/)(?:var|node_modules|\.venv|\.gradle)/)"
)


def findings(path: str, data: bytes) -> list[tuple[str, int]]:
    issues: list[tuple[str, int]] = []
    if PRIVATE_FILE.search(path) and Path(path).name != ".env.example":
        issues.append(("private-or-generated-file", 1))
    if b"\0" in data[:8192]:
        return issues
    for rule, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(data):
            if rule == "credential-literal" and re.search(rb'''["'](?:synthetic|fixture|example)[-_]''', match[0]):
                continue
            issues.append((rule, data.count(b"\n", 0, match.start()) + 1))
    for match in IP_URL.finditer(data):
        try:
            address = ipaddress.ip_address(match[1].decode())
        except ValueError:
            continue
        if address.is_global:
            issues.append(("public-ip-url", data.count(b"\n", 0, match.start()) + 1))
    if path.endswith((".md", ".toml", ".json")) and not path.startswith("tests/"):
        for match in PERSONAL_PATH.finditer(data):
            if match[1].lower() not in (b"user", b"name", b"example", b"public", b"default"):
                issues.append(("personal-home-path", data.count(b"\n", 0, match.start()) + 1))
    return issues


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def working_files() -> Iterator[tuple[str, bytes]]:
    names = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
    for raw in sorted(set(names)):
        if not raw:
            continue
        name = raw.decode("utf-8")
        path = ROOT / name
        if path.is_file():
            yield name, path.read_bytes()


def historical_files() -> Iterator[tuple[str, bytes]]:
    objects = git("rev-list", "--objects", "--all").decode("utf-8").splitlines()
    with subprocess.Popen(
        ["git", "cat-file", "--batch"], cwd=ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        try:
            for item in objects:
                oid, _, path = item.partition(" ")
                process.stdin.write(oid.encode("ascii") + b"\n")
                process.stdin.flush()
                header = process.stdout.readline().split()
                if len(header) != 3:
                    raise RuntimeError("Git object could not be read")
                data = process.stdout.read(int(header[2]))
                if process.stdout.read(1) != b"\n":
                    raise RuntimeError("Incomplete Git object")
                if header[1] == b"blob":
                    yield f"{oid[:12]}:{path}", data
        finally:
            process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError("Git history check failed")


def check(files: Iterator[tuple[str, bytes]], *, history: bool = False) -> int:
    count = 0
    total = 0
    for label, data in files:
        count += 1
        path = label.split(":", 1)[1] if history else label
        for rule, line in findings(path, data):
            print(f"{label}:{line}: {rule}")
            total += 1
    print(f"{'History' if history else 'Working tree'}: {count} files, {total} findings")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="also inspect all reachable local Git history")
    args = parser.parse_args()
    total = check(working_files())
    if args.history:
        total += check(historical_files(), history=True)
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
