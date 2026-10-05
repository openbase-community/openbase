"""Cross-platform process lookup/termination, backed by ``psutil``.

Replaces the previous POSIX-only ``lsof``/``ss``/``ps`` subprocess calls in
``services/launchd.py`` with a single implementation that also works on
Windows (which has none of those tools).
"""

from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Callable

import psutil

# ``signal.SIGKILL`` does not exist on Windows; referencing the attribute
# would raise AttributeError even inside a branch that never runs there.
# Resolve once at import time with the well-known POSIX signal number as a
# fallback so this module always imports cleanly on any platform.
_SIGTERM = signal.SIGTERM
_SIGKILL = getattr(signal, "SIGKILL", 9)


def listening_pids(port: int) -> set[int]:
    """PIDs of processes with a LISTEN socket bound to ``port``."""
    if sys.platform == "darwin":
        # psutil.net_connections needs root on macOS — it raises AccessDenied
        # on the first SIP-protected process it scans. lsof does per-process
        # lookups and works unprivileged.
        return _listening_pids_lsof(port)
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


def _listening_pids_lsof(port: int) -> set[int]:
    import subprocess

    try:
        result = subprocess.run(  # noqa: S603 - fixed argv
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {int(token) for token in result.stdout.split() if token.isdigit()}


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


def process_tree_pids(pid: int) -> set[int]:
    """``pid`` plus every live descendant; empty when ``pid`` is gone."""
    try:
        proc = psutil.Process(pid)
        return {pid, *(child.pid for child in proc.children(recursive=True))}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return set()


def wait_for_pid_change(
    read_pid: Callable[[], int | None],
    old_pid: int | None,
    *,
    timeout: float,
    interval: float = 0.25,
) -> int | None:
    """Poll ``read_pid`` until it differs from ``old_pid`` or ``timeout`` passes.

    Returns the last pid observed (``old_pid`` itself when nothing changed in
    time). Used to wait for a supervised process to exit, and to be
    respawned, without racing its supervisor.
    """
    deadline = time.monotonic() + timeout
    pid = old_pid
    while time.monotonic() < deadline:
        pid = read_pid()
        if pid != old_pid:
            return pid
        time.sleep(interval)
    return pid
