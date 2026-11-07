"""What tests need to know about the host they run on: Linux or native Windows."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

WINDOWS = sys.platform == "win32"

# Where a virtualenv keeps its interpreter.
VENV_PYTHON = Path("Scripts", "python.exe") if WINDOWS else Path("bin", "python")


def shell_command(argv: list[str]) -> str:
    """``argv`` as one command string for the host shell (sh or cmd.exe)."""

    return subprocess.list2cmdline(argv) if WINDOWS else shlex.join(argv)


def link_directory(link: Path, target: Path) -> None:
    """A directory symlink, or a junction where symlinks need a privilege.

    Windows refuses symlinks without Developer Mode or the privilege
    (winerror 1314); a junction is what a user would have there instead.
    """

    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        if getattr(error, "winerror", None) != 1314:
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


def remove_directory_link(link: Path) -> None:
    """Remove a directory symlink or junction, leaving its target."""

    if WINDOWS:
        import os

        os.rmdir(link)
    else:
        link.unlink()


def symlink_file(link: Path, target: Path) -> None:
    """A file symlink; skipped where this Windows session may not make one."""

    try:
        link.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("this Windows session lacks the symlink privilege")
        raise


def alive(pid: int) -> bool:
    """Whether ``pid`` is a live (not zombie, not exited) process."""

    if WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"
