"""Supervise one generated service wrapper where no platform job exists.

The container entrypoint supervises the wrappers that exist when it boots,
each in a restart loop, and it never adopts a service later: a service that
becomes wanted while the workspace runs (the Codex app servers after a switch
to Codex on a cloud workspace, 2026-10-13) had nothing to run it. This module
runs such a service under the same contract the entrypoint keeps, so the
status checks cannot tell the two apart:

* ``<data_dir>/run/<name>.pid`` holds the service's pid while it runs and is
  removed when it exits;
* ``<data_dir>/run/<name>.stopped`` (``services stop``) ends supervision: the
  loop exits instead of relaunching the wrapper.

``<data_dir>/run/<name>.supervisor.pid`` names the supervisor itself, so a
start issued between two relaunches does not spawn a second one.

Usage: ``python -m openbase_coder_cli.services.external_supervisor <name>``.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from openbase_coder_cli.file_lock import LOCK_EX, LOCK_NB, flock
from openbase_coder_cli.paths import (
    DEFAULT_LOG_DIR,
    LAUNCHD_WRAPPER_DIR,
    OPENBASE_BASE_DIR,
)
from openbase_coder_cli.services.mutation_lock import DELEGATION_ENV, DESCRIPTOR_ENV

RUN_DIR = OPENBASE_BASE_DIR / "run"
# Matches the entrypoint's restart loop, which relaunches an exited wrapper
# after this long.
RELAUNCH_DELAY_SECONDS = 5.0
STOP_WAIT_SECONDS = 10.0


def service_pidfile(name: str) -> Path:
    return RUN_DIR / f"{name}.pid"


def supervisor_pidfile(name: str) -> Path:
    return RUN_DIR / f"{name}.supervisor.pid"


def stop_marker(name: str) -> Path:
    return RUN_DIR / f"{name}.stopped"


def _alive_pid(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def supervisor_pid(name: str) -> int | None:
    """The pid of a live supervisor of ``name`` started by this module."""
    return _alive_pid(supervisor_pidfile(name))


def spawn_supervisor(name: str) -> int:
    """Start a detached supervisor for ``name``; returns its pid.

    Detached from the caller (its own session, no inherited service-mutation
    lease), since the caller is a request handler or a restart that ends
    long before the service does.
    """
    env = dict(os.environ)
    env.pop(DELEGATION_ENV, None)
    env.pop(DESCRIPTOR_ENV, None)
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(DEFAULT_LOG_DIR / f"{name}.log", "ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", __name__, name],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    return process.pid


def supervise(name: str, *, relaunch_delay: float = RELAUNCH_DELAY_SECONDS) -> int:
    """Only one fallback loop may own a service, including between launches."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with (RUN_DIR / f"{name}.supervisor.lock").open("a+") as lock:
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                return 0  # The existing loop owns both pidfiles.
            raise
        return _supervise(name, relaunch_delay=relaunch_delay)


def _supervise(name: str, *, relaunch_delay: float) -> int:
    """Run ``name``'s wrapper in a restart loop until it is stopped."""
    wrapper = LAUNCHD_WRAPPER_DIR / f"{name}.sh"
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    supervisor_pidfile(name).write_text(str(os.getpid()), encoding="utf-8")
    child: subprocess.Popen | None = None
    try:
        while not stop_marker(name).exists() and wrapper.is_file():
            with open(DEFAULT_LOG_DIR / f"{name}.log", "ab") as log:
                child = subprocess.Popen(
                    ["bash", str(wrapper)],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            service_pidfile(name).write_text(str(child.pid), encoding="utf-8")
            status = child.wait()
            child = None
            service_pidfile(name).unlink(missing_ok=True)
            print(
                f"[supervisor] exited with status {status}; restarting in "
                f"{relaunch_delay:g}s",
                flush=True,
            )
            deadline = time.monotonic() + relaunch_delay
            while time.monotonic() < deadline and not stop_marker(name).exists():
                time.sleep(min(0.25, relaunch_delay))
        return 0
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=STOP_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        service_pidfile(name).unlink(missing_ok=True)
        supervisor_pidfile(name).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(f"usage: python -m {__name__} <service-name>", file=sys.stderr)
        return 2

    from openbase_coder_cli.services.registry import find_service

    find_service(args[0])  # Reject unknown names before constructing paths.

    def stop(_signum, _frame) -> None:
        # Unwinds through supervise(), which ends the service it runs.
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return supervise(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
