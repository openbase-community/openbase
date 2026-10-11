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
DESCRIPTOR_ENV = "OPENBASE_SERVICE_MUTATION_FD"
_thread_lock = threading.RLock()
_local = threading.local()


class ServiceMutationBusy(TimeoutError):
    pass


def mutation_environment() -> dict[str, str]:
    """Delegate only to an explicitly launched, synchronous child command."""
    environment = dict(os.environ)
    environment.pop(DELEGATION_ENV, None)
    environment.pop(DESCRIPTOR_ENV, None)
    if getattr(_local, "pid", None) == os.getpid():
        environment[DELEGATION_ENV] = _local.token
        descriptor = getattr(_local, "descriptor", None)
        if descriptor is not None:
            environment[DESCRIPTOR_ENV] = str(descriptor)
    return environment


def mutation_descriptors() -> tuple[int, ...]:
    descriptor = getattr(_local, "descriptor", None)
    return (descriptor,) if os.name != "nt" and descriptor is not None else ()


def _inherited_descriptor() -> int | None:
    raw = os.environ.get(DESCRIPTOR_ENV, "")
    if not raw.isdecimal() or os.name == "nt":
        return None
    descriptor = int(raw)
    try:
        stat = os.fstat(descriptor)
        expected = LOCK_PATH.stat()
        if (stat.st_dev, stat.st_ino) != (expected.st_dev, expected.st_ino):
            return None
        # The inherited open-file description keeps the parent's flock alive
        # after parent death; a separately opened descriptor cannot bypass it.
        flock(descriptor, LOCK_EX | LOCK_NB)
    except OSError:
        return None
    return descriptor


def _delegated(token: str) -> bool:
    # Service daemons/grandchildren must acquire their own lease. A token
    # inherited from a finished updater must not bypass the lock either.
    try:
        if LOCK_PATH.read_text() != token:
            return False
    except FileNotFoundError:
        return False
    return token.startswith(f"{os.getppid()}:") or _inherited_descriptor() is not None


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
            _local.descriptor = _inherited_descriptor()
            try:
                yield
            finally:
                _local.pid = None
                _local.descriptor = None
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
                _local.descriptor = handle.fileno() if os.name != "nt" else None
                yield
            finally:
                _local.pid = None
                _local.descriptor = None
                handle.seek(0)
                handle.truncate()
                handle.flush()
                flock(handle, LOCK_UN)
    finally:
        _thread_lock.release()
