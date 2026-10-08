"""Hand one thread's transcript to another computer through the exchange.

Device thread sync already mirrors every recent Codex rollout and Claude
Code session through the exchange folder (``~/.openbase/thread-sync``, an
Openbase Sync root) on a timer. A thread push needs the same transport for
exactly one thread, right now, with a definite answer:

- :func:`export_one` writes this computer's current snapshot of the thread
  and returns its fingerprint (the sending side);
- :func:`snapshot_present` tells the receiving side whether that exact
  snapshot has arrived;
- :func:`import_one` imports exactly that snapshot (the receiving side),
  reusing the periodic importer's safety rules: fast-forward only, never
  over a thread running here, never over a divergent copy.

Folders are translated home-relative by the existing snapshot format
(``source_user_home`` in the metadata), so ``/Users/a/Projects/x`` on a
laptop becomes ``/Users/b/Projects/x`` on the receiving computer.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .thread_sync_common import super_agents_state_db_path

logger = logging.getLogger(__name__)

CODEX = "codex"
CLAUDE_CODE = "claude_code"
SUPPORTED_BACKENDS = (CODEX, CLAUDE_CODE)
LEDGER_LOCK_TIMEOUT_SECONDS = 60.0

# Export skip reasons that mean "the thread is still being written": waiting
# and retrying can succeed. Everything else is a property of the transcript.
_BUSY_EXPORT_REASONS = {"skipped_active", "skipped_unstable"}


class HandoffError(RuntimeError):
    """A handoff step failed; ``code`` is stable, the message is for people."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class HandoffSnapshot:
    backend: str
    entity_id: str
    fingerprint: str
    source_device_id: str


@dataclass(frozen=True)
class HandoffPaths:
    """Where the stores live; tests point these at a temporary home."""

    exchange_dir: Path | None = None
    device_identity_path: Path | None = None
    codex_home: Path | None = None
    codex_ledger_path: Path | None = None
    claude_home: Path | None = None
    claude_ledger_path: Path | None = None
    super_agents_db_path: Path | None = None
    user_home: Path | None = None


def _kwargs(**values):
    return {key: value for key, value in values.items() if value is not None}


def exchange_dir(paths: HandoffPaths | None = None) -> Path:
    from .thread_exchange import DEFAULT_EXCHANGE_DIR

    paths = paths or HandoffPaths()
    return paths.exchange_dir or DEFAULT_EXCHANGE_DIR


def device_id(paths: HandoffPaths | None = None) -> str:
    from .thread_exchange import get_or_create_device_identity

    paths = paths or HandoffPaths()
    identity = get_or_create_device_identity(**_kwargs(path=paths.device_identity_path))
    return identity.device_id


def entity_id_for(backend: str, thread_id: str, backend_session_id: str | None) -> str:
    """The id device sync keys this thread by (Claude: the session id)."""
    if backend == CLAUDE_CODE:
        if not backend_session_id:
            raise HandoffError(
                "not_exportable",
                "This Claude Code thread has no session transcript yet.",
            )
        return backend_session_id
    return thread_id


def _check_backend(backend: str) -> None:
    if backend not in SUPPORTED_BACKENDS:
        raise HandoffError(
            "unsupported_backend",
            "Only Codex and Claude Code threads can be pushed.",
        )


def export_one(
    backend: str, entity_id: str, *, paths: HandoffPaths | None = None
) -> HandoffSnapshot:
    """Write the thread's current snapshot into the exchange folder."""
    _check_backend(backend)
    paths = paths or HandoffPaths()
    snapshot = _export_once(backend, entity_id, paths)
    if not snapshot_present(snapshot, paths=paths):
        # The ledger remembers exporting this fingerprint, but the exchange
        # copy has since been pruned (old threads age out of the folder).
        # Forget that export and write the snapshot again.
        _forget_export(snapshot, paths)
        snapshot = _export_once(backend, entity_id, paths)
        if not snapshot_present(snapshot, paths=paths):
            raise HandoffError(
                "export_failed",
                "The thread's snapshot could not be written to the sync folder.",
            )
    return snapshot


def _ledger_path(backend: str, paths: HandoffPaths) -> Path:
    if backend == CODEX:
        from .thread_exchange import DEFAULT_LEDGER_PATH

        return paths.codex_ledger_path or DEFAULT_LEDGER_PATH
    from .claude_models import DEFAULT_DEVICE_LEDGER_PATH

    return paths.claude_ledger_path or DEFAULT_DEVICE_LEDGER_PATH


def _forget_export(snapshot: HandoffSnapshot, paths: HandoffPaths) -> None:
    from .thread_sync_common import ledger_lock, read_device_ledger, write_json_atomic

    scope = "threads" if snapshot.backend == CODEX else "sessions"
    ledger_path = _ledger_path(snapshot.backend, paths)
    with ledger_lock(ledger_path, timeout_seconds=LEDGER_LOCK_TIMEOUT_SECONDS):
        ledger = read_device_ledger(
            ledger_path,
            scope_key=scope,
            logger=logger,
            malformed_event="thread_handoff event=ledger_malformed",
        )
        entry = ledger.get(scope, {}).get(snapshot.entity_id)
        devices = entry.get("devices") if isinstance(entry, dict) else None
        device = (
            devices.get(snapshot.source_device_id)
            if isinstance(devices, dict)
            else None
        )
        snapshots = device.get("snapshots") if isinstance(device, dict) else None
        if isinstance(snapshots, dict):
            snapshots.pop(snapshot.fingerprint, None)
            write_json_atomic(ledger_path, ledger)


def _export_once(backend: str, entity_id: str, paths: HandoffPaths) -> HandoffSnapshot:
    try:
        if backend == CODEX:
            from .thread_exchange import export_thread_snapshots

            results = export_thread_snapshots(
                **_kwargs(
                    codex_home=paths.codex_home,
                    exchange_dir=paths.exchange_dir,
                    device_identity_path=paths.device_identity_path,
                    ledger_path=paths.codex_ledger_path,
                    source_user_home=paths.user_home,
                ),
                max_age_days=None,
                thread_ids={entity_id},
                # The caller confirmed the thread is idle; the rollout checks
                # (terminal event, not open for write) are the ground truth.
                include_store_active=False,
                ledger_lock_timeout=LEDGER_LOCK_TIMEOUT_SECONDS,
            )
        else:
            from .claude_thread_sync import export_claude_thread_snapshots

            results = export_claude_thread_snapshots(
                **_kwargs(
                    claude_home=paths.claude_home,
                    exchange_dir=paths.exchange_dir,
                    device_identity_path=paths.device_identity_path,
                    ledger_path=paths.claude_ledger_path,
                    super_agents_db_path=paths.super_agents_db_path,
                    source_user_home=paths.user_home,
                ),
                max_age_days=None,
                session_ids={entity_id},
                ledger_lock_timeout=LEDGER_LOCK_TIMEOUT_SECONDS,
            )
    except TimeoutError as exc:
        raise HandoffError(
            "sync_busy",
            "Thread sync is busy on this computer. Try again in a moment.",
            retryable=True,
        ) from exc
    result = next(
        (item for item in results if _entity(item) == entity_id),
        None,
    )
    if result is None:
        # Claude sessions that are being written (or missing) produce no
        # candidate at all.
        raise HandoffError(
            "thread_busy",
            "The thread's transcript is still being written, or was not found. "
            "Wait for it to settle and try again.",
            retryable=True,
        )
    if result.status in {"exported", "already_exported"} and result.fingerprint:
        return HandoffSnapshot(
            backend=backend,
            entity_id=entity_id,
            fingerprint=result.fingerprint,
            source_device_id=device_id(paths),
        )
    if result.reason in _BUSY_EXPORT_REASONS:
        raise HandoffError(
            "thread_busy",
            "The thread is still open in its agent on this computer "
            "(a turn may be running, or a terminal has it open). "
            "Close it or wait for it to finish, then try again.",
            retryable=True,
        )
    raise HandoffError(
        "not_exportable",
        f"This thread's transcript cannot be transferred ({result.reason}).",
    )


def _entity(result) -> str | None:
    return getattr(result, "thread_id", None) or getattr(result, "session_id", None)


def snapshot_dir(
    snapshot: HandoffSnapshot, *, paths: HandoffPaths | None = None
) -> Path:
    return (
        exchange_dir(paths)
        / "devices"
        / snapshot.source_device_id
        / "snapshots"
        / snapshot.entity_id
        / snapshot.fingerprint
    )


def snapshot_present(
    snapshot: HandoffSnapshot, *, paths: HandoffPaths | None = None
) -> bool:
    return (snapshot_dir(snapshot, paths=paths) / "metadata.json").is_file()


def import_one(snapshot: HandoffSnapshot, *, paths: HandoffPaths | None = None) -> str:
    """Import exactly ``snapshot``; returns the outcome reason on success."""
    _check_backend(snapshot.backend)
    paths = paths or HandoffPaths()
    try:
        if snapshot.backend == CODEX:
            from .thread_exchange import import_thread_snapshots

            results = import_thread_snapshots(
                **_kwargs(
                    codex_home=paths.codex_home,
                    exchange_dir=paths.exchange_dir,
                    device_identity_path=paths.device_identity_path,
                    ledger_path=paths.codex_ledger_path,
                    target_user_home=paths.user_home,
                ),
                thread_ids={snapshot.entity_id},
                ledger_lock_timeout=LEDGER_LOCK_TIMEOUT_SECONDS,
            )
        else:
            from .claude_thread_sync import import_claude_thread_snapshots

            results = import_claude_thread_snapshots(
                **_kwargs(
                    claude_home=paths.claude_home,
                    exchange_dir=paths.exchange_dir,
                    device_identity_path=paths.device_identity_path,
                    ledger_path=paths.claude_ledger_path,
                    super_agents_db_path=paths.super_agents_db_path,
                    target_user_home=paths.user_home,
                ),
                session_ids={snapshot.entity_id},
                ledger_lock_timeout=LEDGER_LOCK_TIMEOUT_SECONDS,
            )
    except TimeoutError as exc:
        raise HandoffError(
            "sync_busy",
            "Thread sync is busy on the receiving computer. Try again in a moment.",
            retryable=True,
        ) from exc
    result = next(
        (
            item
            for item in results
            if item.fingerprint == snapshot.fingerprint
            and item.source_device_id == snapshot.source_device_id
        ),
        None,
    )
    if result is None:
        raise HandoffError(
            "snapshot_missing",
            "The thread's transcript has not arrived on the receiving computer yet.",
            retryable=True,
        )
    if result.status in {"imported", "already_imported"}:
        return result.reason
    if result.status == "skipped" and result.reason == "superseded_by_local":
        # The receiving computer already holds this transcript plus more.
        return result.reason
    if result.status == "conflict":
        raise HandoffError(
            "conflict",
            "Both computers changed this thread. Resolve it under Threads → "
            "Sync conflicts on the receiving computer, then push again.",
        )
    if result.reason == "target_active":
        raise HandoffError(
            "target_busy",
            "This thread is running on the receiving computer.",
            retryable=True,
        )
    raise HandoffError(
        "import_failed",
        f"The receiving computer could not import the thread ({result.reason}).",
    )


def local_thread_id(
    snapshot: HandoffSnapshot, *, paths: HandoffPaths | None = None
) -> str | None:
    """This computer's Openbase thread id for an imported snapshot.

    Codex threads keep their id everywhere. Claude Code threads are keyed by
    the Super Agents store row that points at the session, which this
    computer may have minted under a different id than the sender's.
    """
    if snapshot.backend == CODEX:
        return snapshot.entity_id
    paths = paths or HandoffPaths()
    db_path = paths.super_agents_db_path or super_agents_state_db_path()
    if not db_path.is_file():
        return None
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            row = conn.execute(
                "select id from sessions where backend_session_id = ? "
                "order by updated_at desc limit 1",
                (snapshot.entity_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
    return str(row[0]) if row else None
