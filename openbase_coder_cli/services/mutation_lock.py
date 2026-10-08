"""Serialize runtime activation, service batches and managed binary refreshes."""

from __future__ import annotations

import errno
import os
import secrets
import threading
import time
from contextlib import contextmanager

import click

from openbase_coder_cli.file_lock import LOCK_EX, LOCK_NB, LOCK_UN, flock
from openbase_coder_cli.paths import OPENBASE_BASE_DIR

LOCK_PATH = OPENBASE_BASE_DIR / ".service-mutation.lock"
DELEGATION_ENV = "OPENBASE_SERVICE_MUTATION_LEASE"
_thread_lock = threading.RLock()
_local = threading.local()


class ServiceMutationBusy(TimeoutError):
    pass


def mutation_environment() -> dict[str, str]:
    """Delegate only to an explicitly launched, synchronous child command."""
    environment = dict(os.environ)
    environment.pop(DELEGATION_ENV, None)
    if getattr(_local, "pid", None) == os.getpid():
        environment[DELEGATION_ENV] = _local.token
    return environment


def _delegated(token: str) -> bool:
    # Service daemons/grandchildren must acquire their own lease. A token
    # inherited from a finished updater must not bypass the lock either.
    if not token.startswith(f"{os.getppid()}:"):
        return False
    try:
        return LOCK_PATH.read_text() == token
    except FileNotFoundError:
        return False


def _require_current_runtime() -> None:
    from openbase_coder_cli._version import __version__
    from openbase_coder_cli.paths import STANDALONE_CURRENT_DIR
    from openbase_coder_cli.runtime import current_runtime_package

    package = current_runtime_package()
    if package is not None and STANDALONE_CURRENT_DIR.is_symlink():
        if (
            package.root.resolve() != STANDALONE_CURRENT_DIR.resolve()
            or package.version != __version__
        ):
            raise click.ClickException(
                "This process belongs to an older runtime; rerun using the current CLI."
            )


@contextmanager
def service_mutation(*, timeout: float = 120):
    if not _thread_lock.acquire(timeout=timeout):
        raise ServiceMutationBusy("Another service activation is running.")
    try:
        if getattr(_local, "pid", None) == os.getpid():
            yield
            return
        token = os.environ.get(DELEGATION_ENV, "")
        if token and _delegated(token):
            _require_current_runtime()
            _local.pid, _local.token = os.getpid(), token
            try:
                yield
            finally:
                _local.pid = None
            return
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOCK_PATH.open("a+") as handle:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    flock(handle, LOCK_EX | LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES):
                        raise
                    if time.monotonic() >= deadline:
                        raise ServiceMutationBusy(
                            "Another service activation is running."
                        ) from exc
                    time.sleep(0.1)
            try:
                _require_current_runtime()
                token = f"{os.getpid()}:{secrets.token_hex(16)}"
                handle.seek(0)
                handle.truncate()
                handle.write(token)
                handle.flush()
                _local.pid, _local.token = os.getpid(), token
                yield
            finally:
                _local.pid = None
                handle.seek(0)
                handle.truncate()
                handle.flush()
                flock(handle, LOCK_UN)
    finally:
        _thread_lock.release()
