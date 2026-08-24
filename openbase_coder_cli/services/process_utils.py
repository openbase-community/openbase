"""Cross-platform process lookup/termination, backed by ``psutil``.

Replaces the previous POSIX-only ``lsof``/``ss``/``ps`` subprocess calls in
``services/launchd.py`` with a single implementation that also works on
Windows (which has none of those tools).
"""

from __future__ import annotations

import os
import signal
import sys

import psutil

# ``signal.SIGKILL`` does not exist on Windows; referencing the attribute
# would raise AttributeError even inside a branch that never runs there.
# Resolve once at import time with the well-known POSIX signal number as a
# fallback so this module always imports cleanly on any platform.
_SIGTERM = signal.SIGTERM
_SIGKILL = getattr(signal, "SIGKILL", 9)


def listening_pids(port: int) -> set[int]:
    """PIDs of processes with a LISTEN socket bound to ``port``."""
    pids: set[int] = set()
    for conn in psutil.net_connections(kind="inet"):
        if (
            conn.status == psutil.CONN_LISTEN
            and conn.laddr
            and conn.laddr.port == port
            and conn.pid
        ):
            pids.add(conn.pid)
    return pids


def process_cmdline(pid: int) -> str:
    """Space-joined argv of ``pid``, or ``""`` if it can't be read."""
    try:
        return " ".join(psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""


def terminate(pid: int, *, force: bool = False) -> None:
    """Terminate ``pid`` gracefully, or forcefully when ``force`` is set.

    POSIX keeps the previous ``os.kill`` behavior unchanged; Windows has no
    signal-based termination so it goes through ``psutil`` instead.
    """
    if sys.platform == "win32":
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        try:
            proc.kill() if force else proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        return

    try:
        os.kill(pid, _SIGKILL if force else _SIGTERM)
    except ProcessLookupError:
        return
