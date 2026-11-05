"""Overlay views of the project, mounted in mutator's own namespaces.

mutator enters a user and mount namespace once, before any thread starts:
the kernel refuses ``unshare(CLONE_NEWUSER)`` to a threaded process. Inside
it, an unprivileged process may mount overlayfs. Every mount is private to
mutator and its children, and disappears with the process.
(docs/adr/0006-mutants-run-in-overlay-views.md)
"""

from __future__ import annotations

import ctypes
import os
import threading
from pathlib import Path

_CLONE_NEWNS = 0x00020000
_CLONE_NEWUSER = 0x10000000
_MS_REC = 0x4000
_MS_PRIVATE = 0x40000
_MNT_DETACH = 2

UNAVAILABLE = (
    "mutator runs every mutant in a private overlay view and needs unprivileged user "
    "namespaces, which this system refuses ({error}). On Ubuntu 23.10 and later, allow "
    "them with `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`; elsewhere "
    "check `user.max_user_namespaces`."
)

_libc = ctypes.CDLL(None, use_errno=True)
_entered = False


class SandboxError(RuntimeError):
    """The system cannot give mutator a private mount namespace."""


def _check(result: int, what: str) -> None:
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"{what}: {os.strerror(error)}")


def enter() -> None:
    """Enter a private user and mount namespace. Idempotent.

    The current uid and gid map to themselves, so files mutator and its tests
    create keep their owner.
    """

    global _entered
    if _entered:
        return
    if threading.active_count() > 1:
        raise SandboxError("mutator must enter its namespace before starting threads")
    uid, gid = os.getuid(), os.getgid()
    try:
        _check(_libc.unshare(_CLONE_NEWUSER | _CLONE_NEWNS), "unshare")
        Path("/proc/self/setgroups").write_text("deny")
        Path("/proc/self/uid_map").write_text(f"{uid} {uid} 1")
        Path("/proc/self/gid_map").write_text(f"{gid} {gid} 1")
        _check(_libc.mount(b"none", b"/", None, _MS_REC | _MS_PRIVATE, None), "mount --make-rprivate /")
    except OSError as error:
        raise SandboxError(UNAVAILABLE.format(error=error)) from error
    _entered = True


def _option(path: Path) -> str:
    text = str(path)
    if any(character in text for character in ",:\\"):
        raise SandboxError(f"An overlay layer path cannot contain ',', ':' or '\\': {text}")
    return text


def mount_overlay(target: Path, lowers: list[Path], upper: Path, work: Path) -> None:
    """Mount ``lowers`` (top first) under a writable ``upper`` at ``target``."""

    enter()
    options = ",".join([
        "userxattr",
        "lowerdir=" + ":".join(_option(path) for path in lowers),
        "upperdir=" + _option(upper),
        "workdir=" + _option(work),
    ])
    _check(_libc.mount(b"overlay", str(target).encode(), b"overlay", 0, options.encode()),
           f"mount overlay at {target}")


def unmount(target: Path) -> None:
    """Detach the mount at ``target``, even while a leftover process holds it."""

    _check(_libc.umount2(str(target).encode(), _MNT_DETACH), f"umount {target}")


def make_opaque(directory: Path) -> None:
    """Hide the lower layers' contents of ``directory`` in an overlay view."""

    os.setxattr(directory, "user.overlay.opaque", b"y")
