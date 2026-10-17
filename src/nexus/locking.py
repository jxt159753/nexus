"""Portable process ownership of a data directory; no task or device logic."""

import os
from pathlib import Path


class DataDirectoryLock:
    def __init__(self, path: Path):
        if path.is_symlink() or path.is_junction():
            raise ValueError("process lock must not be a link")
        self.file = path.open("a+b")
        try:
            if self.file.seek(0, 2) == 0:
                self.file.write(b"\0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise

    def close(self) -> None:
        self.file.close()
