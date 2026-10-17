"""Windows lifecycle primitives. Job handles are owned only by the Node parent."""

import ctypes
import os
from ctypes import wintypes as w


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", w.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", w.DWORD), ("Affinity", ctypes.c_size_t),
        ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD),
    ]


class IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IOCounters)] + [
        (name, ctypes.c_size_t) for name in (
            "ProcessMemoryLimit", "JobMemoryLimit", "PeakProcessMemoryUsed", "PeakJobMemoryUsed",
        )
    ]


class Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in (
        "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime",
    )] + [(name, w.DWORD) for name in (
        "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses",
    )]


def kernel():
    if os.name != "nt":
        raise OSError("Windows Node requires Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        "OpenJobObjectW": ([w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE),
        "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
        "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
        "SetHandleInformation": ([w.HANDLE, w.DWORD, w.DWORD], w.BOOL),
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "GetCurrentProcess": ([], w.HANDLE),
        "IsProcessInJob": ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        "GetDriveTypeW": ([w.LPCWSTR], w.UINT),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def require_local_drive(anchor: str) -> None:
    if kernel().GetDriveTypeW(anchor) not in {2, 3, 5, 6}:
        raise ValueError("local filesystem drive required")


class WindowsJob:
    def __init__(self, name: str):
        self.api = kernel()
        ctypes.set_last_error(0)
        self.handle = self.api.CreateJobObjectW(None, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if ctypes.get_last_error() == 183:
                raise OSError("job name already exists")
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self.api.SetHandleInformation(self.handle, 1, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def assign(self, process) -> None:
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())


def job_stopped(name: str) -> bool:
    api = kernel()
    handle = api.OpenJobObjectW(4, False, name)  # JOB_OBJECT_QUERY
    if not handle:
        if ctypes.get_last_error() == 2:  # Object no longer exists.
            return True
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = Accounting()
        if not api.QueryInformationJobObject(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.ActiveProcesses == 0
    finally:
        api.CloseHandle(handle)


def require_current_job(name: str) -> None:
    """Also checks redirected Python interpreters belong to the exact parent job."""
    api = kernel()
    handle = api.OpenJobObjectW(4, False, name)
    if not handle:
        raise OSError("worker job absent")
    try:
        member = w.BOOL()
        if not api.IsProcessInJob(api.GetCurrentProcess(), handle, ctypes.byref(member)) or not member.value:
            raise OSError("worker is not managed by the expected job")
    finally:
        # Never retain a child-owned Job handle during tool execution.
        api.CloseHandle(handle)
