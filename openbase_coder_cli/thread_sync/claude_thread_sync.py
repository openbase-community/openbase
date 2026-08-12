"""Sync Claude Code sessions between normal and Openbase-managed config homes."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from openbase_coder_cli.paths import (
    NORMAL_CLAUDE_CONFIG_DIR,
    OPENBASE_CLAUDE_CONFIG_DIR,
)

from .claude_conflict_payloads import (
    claude_thread_home_sync_conflicts_payload,
    claude_thread_snapshot_conflicts_payload,
    claude_thread_sync_conflicts_payload,
    resolve_claude_snapshot_conflict,
)
from .claude_jsonl import _meaningful_user_text
from .claude_ledger import (
    _read_device_ledger,
    _read_sync_ledger,
    _record_conflict,
    _record_synced_pair,
    _write_device_ledger,
    _write_sync_ledger,
)
from .claude_models import (
    DEFAULT_DEVICE_EXCHANGE_DIR,
    DEFAULT_DEVICE_LEDGER_PATH,
    DEFAULT_SYNC_LEDGER_PATH,
    DEFAULT_SYNC_MAX_AGE_DAYS,
    FINGERPRINT_MATCH_KEYS,
    ClaudeConflictResolutionError,
    ClaudeSessionSnapshot,
    ClaudeThreadSnapshotResult,
    ClaudeThreadSyncResult,
)
from .claude_session_db import (
    _active_claude_session_ids,
    _backfill_openbase_session_metadata,
    _translate_super_agent_session_cwds,
    _translated_metadata_cwd,
)
from .claude_snapshot_io import (
    _copy_session_into_home,
    _discover_sessions,
    _fingerprint_id,
    _import_device_snapshot_into_home,
    _read_device_snapshot_metadata,
    _read_session_snapshot,
    _same_fingerprint,
    _validate_device_snapshot,
    _write_device_snapshot,
)
from .thread_exchange import DEFAULT_DEVICE_IDENTITY_PATH
from .thread_import import _rollout_has_prefix
from .thread_sync_common import (
    DeviceIdentity,
    LocalSnapshotState,
    SnapshotExportCandidate,
    SnapshotImportSource,
    device_snapshot_dirs,
    file_content_relation,
    get_or_create_device_identity,
    ledger_sync_decision,
    merged_sync_conflicts_payload,
    read_device_identity,
    run_snapshot_export,
    run_snapshot_import,
    sync_cutoff_ms,
)

__all__ = [
    "ClaudeConflictResolutionError",
    "ClaudeSessionSnapshot",
    "ClaudeThreadSnapshotResult",
    "ClaudeThreadSyncResult",
    "DEFAULT_DEVICE_EXCHANGE_DIR",
    "DEFAULT_DEVICE_LEDGER_PATH",
    "DEFAULT_SYNC_LEDGER_PATH",
    "DEFAULT_SYNC_MAX_AGE_DAYS",
    "DEFAULT_DEVICE_IDENTITY_PATH",
    "claude_thread_home_sync_conflicts_payload",
    "claude_thread_snapshot_conflicts_payload",
    "claude_thread_snapshot_status",
    "claude_thread_sync_conflicts_payload",
    "export_claude_thread_snapshots",
    "import_claude_thread_snapshots",
    "resolve_claude_snapshot_conflict",
    "sync_claude_thread_snapshots_once",
    "sync_claude_threads_once",
    "_meaningful_user_text",
    "merged_sync_conflicts_payload",
]

logger = logging.getLogger(__name__)


def sync_claude_threads_once(
    *,
    normal_home: Path = NORMAL_CLAUDE_CONFIG_DIR,
    openbase_home: Path = OPENBASE_CLAUDE_CONFIG_DIR,
    ledger_path: Path = DEFAULT_SYNC_LEDGER_PATH,
    super_agents_db_path: Path | None = None,
    stability_delay_seconds: float = 0.2,
    max_age_days: int | None = DEFAULT_SYNC_MAX_AGE_DAYS,
    active_session_ids: set[str] | None = None,
) -> list[ClaudeThreadSyncResult]:
    """Run one conservative bidirectional sync pass between Claude Code homes."""
    normal_home = normal_home.expanduser()
    openbase_home = openbase_home.expanduser()
    if not normal_home.exists():
        raise FileNotFoundError(f"Normal Claude config dir not found: {normal_home}")
    openbase_home.mkdir(parents=True, exist_ok=True)

    active_ids = set(active_session_ids or set()) | _active_claude_session_ids(
        super_agents_db_path
    )
    normal_sessions = _discover_sessions(
        normal_home,
        stability_delay_seconds=stability_delay_seconds,
    )
    openbase_sessions = _discover_sessions(
        openbase_home,
        stability_delay_seconds=stability_delay_seconds,
    )
    ledger = _read_sync_ledger(ledger_path)
    cutoff_ms = sync_cutoff_ms(max_age_days)

    results: list[ClaudeThreadSyncResult] = []
    for session_id in _session_ids_by_updated_at(normal_sessions, openbase_sessions):
        normal_snapshot = normal_sessions.get(session_id)
        openbase_snapshot = openbase_sessions.get(session_id)
        try:
            if (
                cutoff_ms is not None
                and _latest_updated_ms(normal_snapshot, openbase_snapshot) < cutoff_ms
            ):
                result = ClaudeThreadSyncResult(
                    session_id, "skipped", None, "skipped_old"
                )
            elif session_id in active_ids:
                result = ClaudeThreadSyncResult(
                    session_id, "skipped", None, "skipped_active"
                )
            else:
                result = _sync_one_session(
                    session_id,
                    normal_snapshot=normal_snapshot,
                    openbase_snapshot=openbase_snapshot,
                    normal_home=normal_home,
                    openbase_home=openbase_home,
                    ledger=ledger,
                )
        except Exception:
            result = ClaudeThreadSyncResult(session_id, "error", None, "error")
            logger.exception("claude_thread_sync event=error session_id=%s", session_id)
        else:
            _log_sync_result(result)
        results.append(result)

        updated_openbase = openbase_sessions.get(session_id)
        if result.direction == "normal_to_openbase" and result.status == "transferred":
            updated_openbase = _read_session_snapshot(
                openbase_home,
                _target_root_path(normal_snapshot.root_path, normal_home, openbase_home)
                if normal_snapshot is not None
                else None,
                stability_delay_seconds=0,
            )
            if updated_openbase is not None:
                openbase_sessions[session_id] = updated_openbase
        if updated_openbase is not None:
            _backfill_openbase_session_metadata(
                updated_openbase,
                db_path=super_agents_db_path,
            )

    _write_sync_ledger(ledger_path, ledger)
    return results


def sync_claude_thread_snapshots_once(
    *,
    openbase_home: Path = OPENBASE_CLAUDE_CONFIG_DIR,
    exchange_dir: Path = DEFAULT_DEVICE_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_DEVICE_LEDGER_PATH,
    super_agents_db_path: Path | None = None,
    stability_delay_seconds: float = 0.2,
    max_age_days: int | None = DEFAULT_SYNC_MAX_AGE_DAYS,
    user_home: Path | None = None,
) -> dict[str, list[ClaudeThreadSnapshotResult]]:
    exports = export_claude_thread_snapshots(
        openbase_home=openbase_home,
        exchange_dir=exchange_dir,
        device_identity_path=device_identity_path,
        ledger_path=ledger_path,
        super_agents_db_path=super_agents_db_path,
        stability_delay_seconds=stability_delay_seconds,
        max_age_days=max_age_days,
        source_user_home=user_home,
    )
    imports = import_claude_thread_snapshots(
        openbase_home=openbase_home,
        exchange_dir=exchange_dir,
        device_identity_path=device_identity_path,
        ledger_path=ledger_path,
        super_agents_db_path=super_agents_db_path,
        target_user_home=user_home,
    )
    return {"exports": exports, "imports": imports}


def export_claude_thread_snapshots(
    *,
    openbase_home: Path = OPENBASE_CLAUDE_CONFIG_DIR,
    exchange_dir: Path = DEFAULT_DEVICE_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_DEVICE_LEDGER_PATH,
    super_agents_db_path: Path | None = None,
    stability_delay_seconds: float = 0.2,
    max_age_days: int | None = DEFAULT_SYNC_MAX_AGE_DAYS,
    active_session_ids: set[str] | None = None,
    source_user_home: Path | None = None,
) -> list[ClaudeThreadSnapshotResult]:
    openbase_home = openbase_home.expanduser()
    openbase_home.mkdir(parents=True, exist_ok=True)
    identity = get_or_create_device_identity(device_identity_path)
    active_ids = set(active_session_ids or set()) | _active_claude_session_ids(
        super_agents_db_path
    )
    ledger = _read_device_ledger(ledger_path)
    sessions = _discover_sessions(
        openbase_home,
        stability_delay_seconds=stability_delay_seconds,
    )
    results = run_snapshot_export(
        candidates=_export_candidates(
            sessions,
            exchange_dir=exchange_dir,
            identity=identity,
            openbase_home=openbase_home,
            active_ids=active_ids,
            cutoff_ms=sync_cutoff_ms(max_age_days),
            source_user_home=source_user_home or Path.home(),
        ),
        device_id=identity.device_id,
        ledger=ledger,
        scope_key="sessions",
        result_factory=ClaudeThreadSnapshotResult,
    )
    _write_device_ledger(ledger_path, ledger)
    return results


def _export_candidates(
    sessions: dict[str, ClaudeSessionSnapshot],
    *,
    exchange_dir: Path,
    identity: DeviceIdentity,
    openbase_home: Path,
    active_ids: set[str],
    cutoff_ms: int | None,
    source_user_home: Path,
) -> Iterator[SnapshotExportCandidate]:
    for snapshot in sorted(
        sessions.values(), key=lambda item: item.updated_at_ms, reverse=True
    ):
        if cutoff_ms is not None and snapshot.updated_at_ms < cutoff_ms:
            yield SnapshotExportCandidate(
                snapshot.session_id, skip_reason="skipped_old"
            )
            continue
        if snapshot.session_id in active_ids:
            yield SnapshotExportCandidate(
                snapshot.session_id, skip_reason="skipped_active"
            )
            continue
        fingerprint_id = _fingerprint_id(snapshot.fingerprint)
        yield SnapshotExportCandidate(
            snapshot.session_id,
            fingerprint_id=fingerprint_id,
            write_snapshot=_device_snapshot_writer(
                exchange_dir=exchange_dir,
                identity=identity,
                openbase_home=openbase_home,
                snapshot=snapshot,
                fingerprint_id=fingerprint_id,
                source_user_home=source_user_home,
            ),
        )


def _device_snapshot_writer(
    *,
    exchange_dir: Path,
    identity: DeviceIdentity,
    openbase_home: Path,
    snapshot: ClaudeSessionSnapshot,
    fingerprint_id: str,
    source_user_home: Path,
) -> Callable[[str | None], Path]:
    def write(parent_fingerprint: str | None) -> Path:
        return _write_device_snapshot(
            exchange_dir=exchange_dir,
            identity=identity,
            openbase_home=openbase_home,
            snapshot=snapshot,
            fingerprint_id=fingerprint_id,
            parent_fingerprint=parent_fingerprint,
            source_user_home=source_user_home,
        )

    return write


def import_claude_thread_snapshots(
    *,
    openbase_home: Path = OPENBASE_CLAUDE_CONFIG_DIR,
    exchange_dir: Path = DEFAULT_DEVICE_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_DEVICE_LEDGER_PATH,
    super_agents_db_path: Path | None = None,
    target_user_home: Path | None = None,
) -> list[ClaudeThreadSnapshotResult]:
    openbase_home = openbase_home.expanduser()
    openbase_home.mkdir(parents=True, exist_ok=True)
    target_home = target_user_home or Path.home()
    _translate_super_agent_session_cwds(super_agents_db_path, target_home)
    identity = get_or_create_device_identity(device_identity_path)
    ledger = _read_device_ledger(ledger_path)
    results = run_snapshot_import(
        exchange_dir=exchange_dir,
        device_id=identity.device_id,
        ledger=ledger,
        source=_device_import_source(
            openbase_home=openbase_home,
            active_ids=_active_claude_session_ids(super_agents_db_path),
            super_agents_db_path=super_agents_db_path,
            target_user_home=target_home,
        ),
        result_factory=ClaudeThreadSnapshotResult,
    )
    _write_device_ledger(ledger_path, ledger)
    return results


def _device_import_source(
    *,
    openbase_home: Path,
    active_ids: set[str],
    super_agents_db_path: Path | None,
    target_user_home: Path,
) -> SnapshotImportSource:
    def load_local(metadata: dict[str, Any]) -> LocalSnapshotState:
        local_snapshot = _read_session_snapshot(
            openbase_home,
            openbase_home / Path(metadata["root_relative_path"]),
            stability_delay_seconds=0,
        )
        local_fingerprint = (
            _fingerprint_id(local_snapshot.fingerprint)
            if local_snapshot is not None
            else None
        )
        return LocalSnapshotState(
            local_snapshot is not None, local_fingerprint, local_snapshot
        )

    def import_blocked_reason(
        metadata: dict[str, Any], _local: LocalSnapshotState
    ) -> str | None:
        if metadata["session_id"] in active_ids:
            return "target_active"
        return None

    def perform_import(
        snapshot_dir: Path, metadata: dict[str, Any], local: LocalSnapshotState
    ) -> str | None:
        try:
            _import_device_snapshot_into_home(
                snapshot_dir=snapshot_dir,
                metadata=metadata,
                openbase_home=openbase_home,
                overwrite=local.exists,
            )
        except Exception:
            logger.exception(
                "claude_thread_device_sync event=import_error session_id=%s "
                "snapshot_path=%s",
                metadata["session_id"],
                snapshot_dir,
            )
            return "import_failed"
        imported_snapshot = _read_session_snapshot(
            openbase_home,
            openbase_home / Path(metadata["root_relative_path"]),
            stability_delay_seconds=0,
        )
        if imported_snapshot is not None:
            _backfill_openbase_session_metadata(
                imported_snapshot,
                db_path=super_agents_db_path,
                cwd_override=_translated_metadata_cwd(metadata, target_user_home),
            )
        return None

    def compare_content(
        snapshot_dir: Path, metadata: dict[str, Any], local: LocalSnapshotState
    ) -> str | None:
        snapshot = local.context
        if snapshot is None:
            return None
        fingerprint = snapshot.fingerprint
        if fingerprint.get("root_sha256") == metadata.get("root_sha256") and (
            fingerprint.get("root_size") == metadata.get("root_size")
        ):
            # Companion-file churn shifts the tree hash while the transcript
            # itself is unchanged; matching transcripts count as converged.
            return "identical"
        remote_root = snapshot_dir / "files" / Path(str(metadata["root_relative_path"]))
        return file_content_relation(snapshot.root_path, remote_root)

    return SnapshotImportSource(
        scope_key="sessions",
        entity_id_key="session_id",
        read_metadata=_read_device_snapshot_metadata,
        metadata_error=ValueError,
        validate_snapshot=_validate_device_snapshot,
        load_local=load_local,
        import_blocked_reason=import_blocked_reason,
        perform_import=perform_import,
        conflict_includes_snapshot_path=True,
        compare_content=compare_content,
    )


def claude_thread_snapshot_status(
    *,
    exchange_dir: Path = DEFAULT_DEVICE_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_DEVICE_LEDGER_PATH,
) -> dict[str, Any]:
    identity = read_device_identity(device_identity_path)
    ledger = _read_device_ledger(ledger_path)
    conflicts = [
        {"session_id": session_id, **value["conflict"]}
        for session_id, value in ledger.get("sessions", {}).items()
        if isinstance(value, dict) and isinstance(value.get("conflict"), dict)
    ]
    snapshots = list(device_snapshot_dirs(exchange_dir))
    return {
        "device": identity.to_json() if identity else None,
        "exchange_dir": str(exchange_dir),
        "ledger_path": str(ledger_path),
        "snapshot_count": len(snapshots),
        "session_count": len(ledger.get("sessions", {})),
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
    }


def _sync_one_session(
    session_id: str,
    *,
    normal_snapshot: ClaudeSessionSnapshot | None,
    openbase_snapshot: ClaudeSessionSnapshot | None,
    normal_home: Path,
    openbase_home: Path,
    ledger: dict[str, Any],
) -> ClaudeThreadSyncResult:
    if normal_snapshot is not None and openbase_snapshot is None:
        return _transfer_session(
            normal_snapshot,
            source_home=normal_home,
            target_home=openbase_home,
            direction="normal_to_openbase",
            reason="synced_to_openbase",
            ledger=ledger,
            overwrite=False,
        )
    if openbase_snapshot is not None and normal_snapshot is None:
        return _transfer_session(
            openbase_snapshot,
            source_home=openbase_home,
            target_home=normal_home,
            direction="openbase_to_normal",
            reason="synced_to_normal",
            ledger=ledger,
            overwrite=False,
        )
    if normal_snapshot is None or openbase_snapshot is None:
        return ClaudeThreadSyncResult(session_id, "skipped", None, "not_found")

    normal_fp = normal_snapshot.fingerprint
    openbase_fp = openbase_snapshot.fingerprint
    if _same_fingerprint(normal_fp, openbase_fp):
        _record_synced_pair(ledger, session_id, normal_fp, openbase_fp, "same_content")
        return ClaudeThreadSyncResult(
            session_id, "already_synced", None, "same_content"
        )

    append_only_result = _sync_append_only_prefix_conflict(
        normal_snapshot=normal_snapshot,
        openbase_snapshot=openbase_snapshot,
        normal_home=normal_home,
        openbase_home=openbase_home,
        ledger=ledger,
    )
    if append_only_result is not None:
        return append_only_result

    decision = ledger_sync_decision(
        ledger.get(session_id),
        left_key="normal",
        right_key="openbase",
        left_fingerprint=normal_fp,
        right_fingerprint=openbase_fp,
        fingerprint_keys=FINGERPRINT_MATCH_KEYS,
    )
    if decision in {"both_changed", "conflict_unresolved"}:
        reason = (
            "both_homes_changed"
            if decision == "both_changed"
            else "conflict_unresolved"
        )
        _record_conflict(ledger, session_id, normal_fp, openbase_fp, reason)
        return ClaudeThreadSyncResult(session_id, "conflict", None, reason)
    if decision == "left_changed":
        return _transfer_session(
            normal_snapshot,
            source_home=normal_home,
            target_home=openbase_home,
            direction="normal_to_openbase",
            reason="synced_to_openbase",
            ledger=ledger,
            overwrite=True,
        )
    if decision == "right_changed":
        return _transfer_session(
            openbase_snapshot,
            source_home=openbase_home,
            target_home=normal_home,
            direction="openbase_to_normal",
            reason="synced_to_normal",
            ledger=ledger,
            overwrite=True,
        )
    return ClaudeThreadSyncResult(session_id, "already_synced", None, "ledger_current")


def _sync_append_only_prefix_conflict(
    *,
    normal_snapshot: ClaudeSessionSnapshot,
    openbase_snapshot: ClaudeSessionSnapshot,
    normal_home: Path,
    openbase_home: Path,
    ledger: dict[str, Any],
) -> ClaudeThreadSyncResult | None:
    normal_size = int(normal_snapshot.fingerprint.get("root_size") or 0)
    openbase_size = int(openbase_snapshot.fingerprint.get("root_size") or 0)
    if normal_size == openbase_size:
        return None
    if normal_size > openbase_size:
        if not _rollout_has_prefix(
            normal_snapshot.root_path, openbase_snapshot.root_path
        ):
            return None
        return _transfer_session(
            normal_snapshot,
            source_home=normal_home,
            target_home=openbase_home,
            direction="normal_to_openbase",
            reason="synced_append_only_to_openbase",
            ledger=ledger,
            overwrite=True,
        )
    if not _rollout_has_prefix(openbase_snapshot.root_path, normal_snapshot.root_path):
        return None
    return _transfer_session(
        openbase_snapshot,
        source_home=openbase_home,
        target_home=normal_home,
        direction="openbase_to_normal",
        reason="synced_append_only_to_normal",
        ledger=ledger,
        overwrite=True,
    )


def _transfer_session(
    snapshot: ClaudeSessionSnapshot,
    *,
    source_home: Path,
    target_home: Path,
    direction: str,
    reason: str,
    ledger: dict[str, Any],
    overwrite: bool,
) -> ClaudeThreadSyncResult:
    target_root = _target_root_path(snapshot.root_path, source_home, target_home)
    if target_root.exists() and not overwrite:
        return ClaudeThreadSyncResult(
            snapshot.session_id,
            "skipped",
            direction,
            "target_exists",
            str(snapshot.root_path),
            str(target_root),
        )
    _copy_session_into_home(
        source_home=source_home,
        source_root=snapshot.root_path,
        target_home=target_home,
        overwrite=overwrite,
    )

    target_snapshot = _read_session_snapshot(
        target_home,
        target_root,
        stability_delay_seconds=0,
    )
    target_fp = target_snapshot.fingerprint if target_snapshot else snapshot.fingerprint
    if direction == "normal_to_openbase":
        _record_synced_pair(
            ledger,
            snapshot.session_id,
            snapshot.fingerprint,
            target_fp,
            reason,
        )
    else:
        _record_synced_pair(
            ledger,
            snapshot.session_id,
            target_fp,
            snapshot.fingerprint,
            reason,
        )
    return ClaudeThreadSyncResult(
        snapshot.session_id,
        "transferred",
        direction,
        reason,
        str(snapshot.root_path),
        str(target_root),
    )


def _target_root_path(source_root: Path, source_home: Path, target_home: Path) -> Path:
    return target_home / source_root.relative_to(source_home)


def _session_ids_by_updated_at(
    normal_sessions: dict[str, ClaudeSessionSnapshot],
    openbase_sessions: dict[str, ClaudeSessionSnapshot],
) -> list[str]:
    def updated_at(session_id: str) -> int:
        values = []
        for sessions in (normal_sessions, openbase_sessions):
            snapshot = sessions.get(session_id)
            if snapshot:
                values.append(snapshot.updated_at_ms)
        return max(values or [0])

    return sorted(
        set(normal_sessions) | set(openbase_sessions),
        key=updated_at,
        reverse=True,
    )


def _latest_updated_ms(*snapshots: ClaudeSessionSnapshot | None) -> int:
    return max(
        (snapshot.updated_at_ms for snapshot in snapshots if snapshot), default=0
    )


def _log_sync_result(result: ClaudeThreadSyncResult) -> None:
    if result.status not in {"transferred", "conflict", "error"}:
        return
    message = (
        "claude_thread_sync event=%s session_id=%s direction=%s reason=%s "
        "source=%s target=%s"
    )
    args = (
        result.status,
        result.session_id,
        result.direction,
        result.reason,
        result.source_path,
        result.target_path,
    )
    if result.status == "conflict":
        logger.warning(message, *args)
    elif result.status == "error":
        logger.error(message, *args)
    else:
        logger.info(message, *args)
