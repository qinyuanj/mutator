"""Windows Job Objects: one per command, so its whole process tree can be ended.

A command starts suspended, joins a kill-on-close job, and only then runs, so
no child it starts can escape the job. Terminating the job ends every process
in it; closing its last handle does the same, which also covers a mutator that
was killed itself. (docs/adr/0007-windows-workers-run-in-copies.md)
"""

from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200

_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_BASIC_ACCOUNTING = 1
_BASIC_PROCESS_ID_LIST = 3
_EXTENDED_LIMIT = 9
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_READ = 0x0010


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_kernel32.QueryInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]


class Job:
    """A kill-on-close job, optionally capping each process's committed memory."""

    def __init__(self, process_memory: int | None = None):
        self._handle = _kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if process_memory:
            info.BasicLimitInformation.LimitFlags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = process_memory
        if not _kernel32.SetInformationJobObject(
                self._handle, _EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def adopt(self, process: subprocess.Popen) -> None:
        """Put a process started suspended in the job, then resume it."""

        handle = int(process._handle)  # Popen has no public accessor for it.
        if not _kernel32.AssignProcessToJobObject(self._handle, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        # Popen closed the primary thread handle; resume through the process.
        if _ntdll.NtResumeProcess(handle) != 0:
            raise OSError("cannot resume the test process")

    def terminate(self, code: int = 1) -> None:
        if self._handle:
            _kernel32.TerminateJobObject(self._handle, code)

    def active(self) -> int:
        """Processes still running in the job."""

        if not self._handle:
            return 0
        info = _Accounting()
        if not _kernel32.QueryInformationJobObject(
                self._handle, _BASIC_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None):
            return 0
        return info.ActiveProcesses

    def _pids(self) -> list[int]:
        capacity = 256

        class _List(ctypes.Structure):
            _fields_ = [("Assigned", wintypes.DWORD), ("Listed", wintypes.DWORD),
                        ("Ids", ctypes.c_size_t * capacity)]

        listing = _List()
        if not _kernel32.QueryInformationJobObject(
                self._handle, _BASIC_PROCESS_ID_LIST, ctypes.byref(listing), ctypes.sizeof(listing), None):
            return []
        return list(listing.Ids[:listing.Listed])

    def resident(self) -> int:
        """Working-set bytes of every process in the job."""

        total = 0
        for pid in self._pids() if self._handle else []:
            handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, False, pid)
            if not handle:
                continue
            try:
                counters = _MemoryCounters()
                counters.cb = ctypes.sizeof(counters)
                if _kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    total += counters.WorkingSetSize
            finally:
                _kernel32.CloseHandle(handle)
        return total

    def close(self) -> None:
        """Close the job; whatever still runs in it is ended."""

        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None
