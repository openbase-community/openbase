"""Cross-platform advisory file locking.

POSIX platforms delegate straight to :func:`fcntl.flock`. Windows has no
``fcntl`` module, so it locks a one-byte region at offset 0 through
``msvcrt.locking`` instead. That gives the same mutual exclusion for the
single-writer lock files this CLI uses, and it keeps ``OSError`` as the
failure mode callers already handle for non-blocking acquisition.
"""

from __future__ import annotations

import os
import sys
import time
from typing import IO, Union

_WINDOWS = sys.platform == "win32"

if _WINDOWS:
    import msvcrt

    # Mirror the POSIX flock constants so call sites read identically on
    # both platforms.
    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8
else:
    import fcntl

    LOCK_EX = fcntl.LOCK_EX
    LOCK_NB = fcntl.LOCK_NB
    LOCK_UN = fcntl.LOCK_UN

# How long to wait between blocking-lock retries on Windows.
_RETRY_INTERVAL_SECONDS = 0.1

FileOrDescriptor = Union[int, IO[bytes], IO[str]]


def _descriptor(handle: FileOrDescriptor) -> int:
    return handle if isinstance(handle, int) else handle.fileno()


def flock(handle: FileOrDescriptor, operation: int) -> None:
    """Apply ``operation`` to ``handle``, following ``fcntl.flock`` semantics.

    ``handle`` may be a file descriptor or any object exposing ``fileno()``.
    Raises ``OSError`` when ``LOCK_NB`` is set and the lock is already held.
    """
    descriptor = _descriptor(handle)
    if not _WINDOWS:
        fcntl.flock(descriptor, operation)
        return
    _windows_flock(descriptor, operation)


def _windows_flock(descriptor: int, operation: int) -> None:
    # msvcrt.locking works on the region starting at the current file
    # position, so pin it to offset 0 and restore the caller's position.
    previous_position = os.lseek(descriptor, 0, os.SEEK_CUR)
    os.lseek(descriptor, 0, os.SEEK_SET)
    try:
        if operation & LOCK_UN:
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            return
        if operation & LOCK_NB:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        # LK_LOCK gives up after roughly ten seconds; POSIX LOCK_EX waits
        # indefinitely, so retry until the lock is ours.
        while True:
            try:
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(_RETRY_INTERVAL_SECONDS)
    finally:
        os.lseek(descriptor, previous_position, os.SEEK_SET)
