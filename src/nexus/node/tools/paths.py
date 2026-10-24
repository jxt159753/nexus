"""Local path guards. On Windows held directory handles deny rename/delete."""

import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path

from nexus.contracts.basic import relative_path
from nexus.node.config import local_directory


def ordinary(info, *, directory=False):
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise PermissionError("links and special files are not allowed")


@contextmanager
def hold_directory(path: Path):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        # OPEN_REPARSE_POINT + BACKUP_SEMANTICS, no FILE_SHARE_DELETE.
        handle = api.CreateFileW(str(path), 0x80, 3, None, 3, 0x02200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ordinary(path.lstat(), directory=True)
            yield path
        finally:
            api.CloseHandle(handle)
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            ordinary(os.fstat(descriptor), directory=True)
            yield path
        finally:
            os.close(descriptor)


@contextmanager
def guarded_path(root: Path, relative: str):
    relative_path(relative)
    local_directory(root)
    with ExitStack() as stack:
        # Lock ancestors first so no previously checked component can be swapped.
        for directory in (*reversed(root.parents), root):
            stack.enter_context(hold_directory(directory))
        target = root
        for part in relative.split("/")[:-1]:
            target = target / part
            stack.enter_context(hold_directory(target))
        yield target / relative.split("/")[-1]


@contextmanager
def read_handle(path: Path):
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            error = ctypes.get_last_error()
            if error in {2, 3}:
                raise FileNotFoundError(str(path))
            raise ctypes.WinError(error)
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            api.CloseHandle(handle)
            raise
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        ordinary(info)
        if info.st_nlink != 1:
            raise PermissionError("hard-linked files are not allowed")
        yield stream


def replace_file(source, target):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.ReplaceFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
        api.ReplaceFileW.restype = wintypes.BOOL
        # Preserve the destination ACL/attributes; never ignore merge errors.
        if not api.ReplaceFileW(str(target), str(source), None, 0, None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)
