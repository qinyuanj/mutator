"""Exclusive, non-blocking locks on an open file, released when the process dies."""

from __future__ import annotations

import sys

if sys.platform == "win32":
    import msvcrt
    import os

    def try_lock(descriptor: int) -> bool:
        """Lock the file's first byte; False when another process holds it."""

        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def unlock(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def try_lock(descriptor: int) -> bool:
        """flock the file; False when another process holds it."""

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def unlock(descriptor: int) -> None:
        """Closing the descriptor releases the flock."""
