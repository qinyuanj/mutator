"""What tests need to know about the host they run on: Linux or native Windows."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

WINDOWS = sys.platform == "win32"

# Where a virtualenv keeps its interpreter.
VENV_PYTHON = Path("Scripts", "python.exe") if WINDOWS else Path("bin", "python")

# A toolchain the tests need. A skip reads as a pass, so CI sets
# MUTATOR_REQUIRE_TOOLCHAINS=1 and a missing toolchain fails instead.
REQUIRE_TOOLCHAINS = os.environ.get("MUTATOR_REQUIRE_TOOLCHAINS") == "1"


def require_toolchain(ready: bool, reason: str) -> None:
    """Skip for a missing toolchain, or fail where a skip would read as a pass."""

    if ready:
        return
    if REQUIRE_TOOLCHAINS:
        pytest.fail(f"{reason}; MUTATOR_REQUIRE_TOOLCHAINS=1 makes that a failure")
    pytest.skip(reason)


def lua_utf8_name_gap(lua: str, directory: Path) -> str | None:
    """Why this Lua cannot open a UTF-8 file name from UTF-8 source, or None.

    A native Lua distribution may take the ANSI code page for file names (ADR
    0007: "Native Lua may be unable to open non-ANSI source paths"), so a
    module named in a UTF-8 specification is unopenable unless that code page
    is UTF-8. LuaCov's untested-file scan goes through the same APIs, so a
    selected-but-unrequired source named that way gets no zero-hit record
    either. Measured with the loadfile a specification's `require` performs,
    so the stated reason is this host's behaviour and not a platform guess.
    """

    probe = directory / "utf8-name-probe"
    probe.mkdir(parents=True, exist_ok=True)
    name = "probe \u00fc.lua"
    (probe / name).write_text("return 1\n", encoding="utf-8")
    script = probe / "probe.lua"
    script.write_text(f"io.write(tostring(loadfile([==[{name}]==]) ~= nil))\n", encoding="utf-8")
    result = subprocess.run([lua, script.name], cwd=probe, capture_output=True)
    if result.returncode == 0 and result.stdout == b"true":
        return None
    return (
        f"Lua cannot open the UTF-8 file name {name!r} on this host, so a UTF-8 "
        "module is not requireable and LuaCov records no zero-hit entry for one: "
        "a native Lua distribution may take the ANSI code page for file names "
        "(ADR 0007). Passing needs a Lua that opens UTF-8 names - the Windows "
        "UTF-8 code page option, or a distribution that calls the wide-char "
        "APIs. Stated here, not skipped silently (#16 item 3, #18)."
    )


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
