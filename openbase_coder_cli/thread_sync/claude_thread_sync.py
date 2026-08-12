"""Sync Claude Code sessions between normal and Openbase-managed config homes."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openbase_coder_cli.paths import (
    NORMAL_CLAUDE_CONFIG_DIR,
    OPENBASE_BASE_DIR,
    OPENBASE_CLAUDE_CONFIG_DIR,
)

from .thread_exchange import DEFAULT_DEVICE_IDENTITY_PATH
from .thread_import import _rollout_has_prefix, _rollout_open_for_write, _string
from .thread_sync_common import (
    DeviceIdentity,
    LocalSnapshotState,
    SnapshotExportCandidate,
    SnapshotImportSource,
    collect_snapshot_records,
    device_snapshot_dirs,
    file_content_relation,
    find_snapshot_record,
    get_or_create_device_identity,
    ledger_sync_decision,
    merged_sync_conflicts_payload,
    path_stable,
    read_device_identity,
    read_device_ledger,
    read_scoped_ledger,
    record_device_snapshot,
    record_sync_conflict,
    record_synced_pair,
    remove_empty_dir,
    run_snapshot_export,
    run_snapshot_import,
    super_agents_state_db_path,
    sync_cutoff_ms,
    translate_home_path,
    write_json_atomic,
    write_scoped_ledger,
)

SCHEMA_VERSION = 1
CLAUDE_SYNC_LEDGER_NAME = "claude-thread-sync-ledger.json"
CLAUDE_DEVICE_LEDGER_NAME = "claude-thread-device-sync-ledger.json"
# Shared with codex: one transported product-state exchange folder
# carries both backends (importers skip the other backend's snapshots).
DEFAULT_DEVICE_EXCHANGE_DIR = OPENBASE_BASE_DIR / "thread-sync"
DEFAULT_DEVICE_LEDGER_PATH = OPENBASE_BASE_DIR / CLAUDE_DEVICE_LEDGER_NAME
DEFAULT_SYNC_LEDGER_PATH = OPENBASE_BASE_DIR / CLAUDE_SYNC_LEDGER_NAME
DEFAULT_SYNC_MAX_AGE_DAYS = 15
IMPORT_STAGING_DIR_NAME = ".claude-thread-sync-staging"
IMPORT_BACKUP_DIR_NAME = ".claude-thread-sync-backups"
DEFAULT_LEGACY_SUPER_AGENTS_STATE_PATH = Path.home() / ".super-agents" / "state.json"
CLAUDE_EVENT_TYPES = {
    "assistant",
    "attachment",
    "file-history-snapshot",
    "last-prompt",
    "permission-mode",
    "queue-operation",
    "system",
    "user",
}
FINGERPRINT_MATCH_KEYS = ("root_sha256", "root_size", "tree_sha256")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaudeSessionSnapshot:
    session_id: str
    project_key: str
    root_path: Path
    relative_root: Path
    cwd: str | None
    name: str
    latest_assistant_message: str | None
    created_at_ms: int | None
    updated_at_ms: int
    fingerprint: dict[str, Any]


@dataclass(frozen=True)
class ClaudeThreadSyncResult:
    session_id: str
    status: str
    direction: str | None
    reason: str
    source_path: str | None = None
    target_path: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "direction": self.direction,
            "reason": self.reason,
            "source_path": self.source_path,
            "target_path": self.target_path,
        }


@dataclass(frozen=True)
class ClaudeThreadSnapshotResult:
    session_id: str
    status: str
    reason: str
    snapshot_path: str | None = None
    fingerprint: str | None = None
    source_device_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "reason": self.reason,
            "snapshot_path": self.snapshot_path,
            "fingerprint": self.fingerprint,
            "source_device_id": self.source_device_id,
        }


class ClaudeConflictResolutionError(ValueError):
    """Raised when a Claude session sync conflict cannot be resolved."""


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


def claude_thread_snapshot_conflicts_payload(
    *,
    openbase_home: Path = OPENBASE_CLAUDE_CONFIG_DIR,
    exchange_dir: Path = DEFAULT_DEVICE_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_DEVICE_LEDGER_PATH,
) -> dict[str, Any]:
    """Show unresolved cross-device Claude session snapshot sync conflicts."""
    openbase_home = openbase_home.expanduser()
    identity = read_device_identity(device_identity_path)
    ledger = _read_device_ledger(ledger_path)
    conflicts: list[dict[str, Any]] = []
    for session_id, session_ledger in ledger.get("sessions", {}).items():
        if not isinstance(session_id, str) or not isinstance(session_ledger, dict):
            continue
        conflict = session_ledger.get("conflict")
        if not isinstance(conflict, dict):
            continue
        source_device_id = _string(conflict.get("source_device_id"))
        snapshots = _snapshot_records(
            exchange_dir,
            session_id=session_id,
            source_device_id=source_device_id,
        )
        incoming_snapshot = _snapshot_payload(
            find_snapshot_record(
                snapshots,
                _string(conflict.get("incoming_fingerprint")),
            )
        )
        latest_remote = _snapshot_payload(_latest_snapshot_record(snapshots))
        local_snapshot = _read_session_snapshot(
            openbase_home,
            _find_local_session_root(openbase_home, session_id),
            stability_delay_seconds=0,
        )
        local_fingerprint = _optional_fingerprint_id(local_snapshot)
        title = (
            _string((latest_remote or {}).get("title"))
            or _string((incoming_snapshot or {}).get("title"))
            or (local_snapshot.name if local_snapshot else None)
            or session_id
        )
        cwd = (
            _string((latest_remote or {}).get("cwd"))
            or _string((incoming_snapshot or {}).get("cwd"))
            or (local_snapshot.cwd if local_snapshot else None)
        )
        conflicts.append(
            {
                "id": f"device:{session_id}",
                "source_type": "device",
                "session_id": session_id,
                "title": title,
                "cwd": cwd,
                "reason": _string(conflict.get("reason")) or "conflict",
                "detected_at": conflict.get("detected_at"),
                "source_device_id": source_device_id,
                "source_device_name": _string(
                    (latest_remote or incoming_snapshot or {}).get("source_device_name")
                ),
                "local_fingerprint": conflict.get("local_fingerprint"),
                "current_local_fingerprint": local_fingerprint,
                "incoming_fingerprint": conflict.get("incoming_fingerprint"),
                "local": _local_session_payload(local_snapshot, local_fingerprint),
                "incoming_snapshot": incoming_snapshot,
                "latest_remote_snapshot": latest_remote,
                "is_resolvable": True,
            }
        )

    return {
        "device": identity.to_json() if identity else None,
        "exchange_dir": str(exchange_dir),
        "ledger_path": str(ledger_path),
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
    }


def claude_thread_home_sync_conflicts_payload(
    *,
    normal_home: Path = NORMAL_CLAUDE_CONFIG_DIR,
    openbase_home: Path = OPENBASE_CLAUDE_CONFIG_DIR,
    ledger_path: Path = DEFAULT_SYNC_LEDGER_PATH,
) -> dict[str, Any]:
    """Show unresolved Claude session sync conflicts between local homes."""
    normal_home = normal_home.expanduser()
    openbase_home = openbase_home.expanduser()
    ledger = _read_sync_ledger(ledger_path)
    conflicts: list[dict[str, Any]] = []
    for session_id, session_ledger in ledger.items():
        if not isinstance(session_id, str) or not isinstance(session_ledger, dict):
            continue
        if session_ledger.get("status") != "conflict":
            continue
        normal_snapshot = _read_session_snapshot(
            normal_home,
            _find_local_session_root(normal_home, session_id),
            stability_delay_seconds=0,
        )
        openbase_snapshot = _read_session_snapshot(
            openbase_home,
            _find_local_session_root(openbase_home, session_id),
            stability_delay_seconds=0,
        )
        normal_fingerprint = _optional_fingerprint_id(normal_snapshot)
        openbase_fingerprint = _optional_fingerprint_id(openbase_snapshot)
        title = (
            (openbase_snapshot.name if openbase_snapshot else None)
            or (normal_snapshot.name if normal_snapshot else None)
            or session_id
        )
        cwd = (openbase_snapshot.cwd if openbase_snapshot else None) or (
            normal_snapshot.cwd if normal_snapshot else None
        )
        conflicts.append(
            {
                "id": f"home:{session_id}",
                "source_type": "home",
                "session_id": session_id,
                "title": title,
                "cwd": cwd,
                "reason": _string(session_ledger.get("reason")) or "conflict",
                "detected_at": session_ledger.get("synced_at"),
                "normal_fingerprint": normal_fingerprint,
                "openbase_fingerprint": openbase_fingerprint,
                "local_fingerprint": openbase_fingerprint,
                "current_local_fingerprint": openbase_fingerprint,
                "normal": _local_session_payload(normal_snapshot, normal_fingerprint),
                "openbase": _local_session_payload(
                    openbase_snapshot, openbase_fingerprint
                ),
                "local": _local_session_payload(
                    openbase_snapshot, openbase_fingerprint
                ),
                "remote_label": "Normal Claude home",
                "is_resolvable": False,
            }
        )

    return {
        "ledger_path": str(ledger_path),
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
    }


def claude_thread_sync_conflicts_payload(
    *,
    normal_home: Path = NORMAL_CLAUDE_CONFIG_DIR,
    openbase_home: Path = OPENBASE_CLAUDE_CONFIG_DIR,
    home_ledger_path: Path = DEFAULT_SYNC_LEDGER_PATH,
    exchange_dir: Path = DEFAULT_DEVICE_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    device_ledger_path: Path = DEFAULT_DEVICE_LEDGER_PATH,
) -> dict[str, Any]:
    """Show unresolved Claude session sync conflicts across homes and devices."""
    return merged_sync_conflicts_payload(
        claude_thread_home_sync_conflicts_payload(
            normal_home=normal_home,
            openbase_home=openbase_home,
            ledger_path=home_ledger_path,
        ),
        claude_thread_snapshot_conflicts_payload(
            openbase_home=openbase_home,
            exchange_dir=exchange_dir,
            device_identity_path=device_identity_path,
            ledger_path=device_ledger_path,
        ),
    )


def resolve_claude_snapshot_conflict(
    session_id: str,
    *,
    action: str,
    openbase_home: Path = OPENBASE_CLAUDE_CONFIG_DIR,
    exchange_dir: Path = DEFAULT_DEVICE_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_DEVICE_LEDGER_PATH,
    super_agents_db_path: Path | None = None,
    target_user_home: Path | None = None,
) -> dict[str, Any]:
    """Resolve one cross-device Claude session snapshot sync conflict."""
    if action not in {"accept_local", "accept_remote_latest"}:
        raise ClaudeConflictResolutionError("unsupported_resolution_action")

    openbase_home = openbase_home.expanduser()
    ledger = _read_device_ledger(ledger_path)
    session_ledger = ledger.get("sessions", {}).get(session_id)
    if not isinstance(session_ledger, dict) or not isinstance(
        session_ledger.get("conflict"), dict
    ):
        raise ClaudeConflictResolutionError("conflict_not_found")
    if session_id in _active_claude_session_ids(super_agents_db_path):
        raise ClaudeConflictResolutionError("session_active")

    conflict = session_ledger["conflict"]
    source_device_id = _string(conflict.get("source_device_id"))
    if not source_device_id:
        raise ClaudeConflictResolutionError("source_device_not_found")

    local_snapshot = _read_session_snapshot(
        openbase_home,
        _find_local_session_root(openbase_home, session_id),
        stability_delay_seconds=0,
    )
    local_fingerprint = _optional_fingerprint_id(local_snapshot)
    snapshots = _snapshot_records(
        exchange_dir,
        session_id=session_id,
        source_device_id=source_device_id,
    )
    if not snapshots:
        raise ClaudeConflictResolutionError("source_snapshots_not_found")

    if action == "accept_remote_latest":
        target_home = target_user_home or Path.home()
        latest = _latest_snapshot_record(snapshots)
        if latest is None:
            raise ClaudeConflictResolutionError("source_snapshots_not_found")
        validation_error = _validate_device_snapshot(latest["path"], latest["metadata"])
        if validation_error:
            raise ClaudeConflictResolutionError(validation_error)
        _import_device_snapshot_into_home(
            snapshot_dir=latest["path"],
            metadata=latest["metadata"],
            openbase_home=openbase_home,
            overwrite=local_snapshot is not None,
        )
        imported_snapshot = _read_session_snapshot(
            openbase_home,
            openbase_home / Path(str(latest["metadata"]["root_relative_path"])),
            stability_delay_seconds=0,
        )
        if imported_snapshot is not None:
            _backfill_openbase_session_metadata(
                imported_snapshot,
                db_path=super_agents_db_path,
                cwd_override=_translated_metadata_cwd(latest["metadata"], target_home),
            )
        resolved_fingerprint = _string(latest["metadata"].get("fingerprint"))
        for snapshot in snapshots:
            record_device_snapshot(
                session_ledger,
                device_id=source_device_id,
                fingerprint_id=snapshot["metadata"]["fingerprint"],
                snapshot_path=snapshot["path"],
                status="imported",
            )
    else:
        if not local_fingerprint:
            raise ClaudeConflictResolutionError("local_session_not_found")
        resolved_fingerprint = local_fingerprint
        for snapshot in snapshots:
            record_device_snapshot(
                session_ledger,
                device_id=source_device_id,
                fingerprint_id=snapshot["metadata"]["fingerprint"],
                snapshot_path=snapshot["path"],
                status="ignored",
            )

    session_ledger.pop("conflict", None)
    session_ledger["local_fingerprint"] = resolved_fingerprint
    session_ledger["resolved_conflict"] = {
        "action": action,
        "resolved_at": time.time(),
        "source_device_id": source_device_id,
        "fingerprint": resolved_fingerprint,
    }
    _write_device_ledger(ledger_path, ledger)
    return {
        "session_id": session_id,
        "action": action,
        "fingerprint": resolved_fingerprint,
        "conflicts": claude_thread_snapshot_conflicts_payload(
            openbase_home=openbase_home,
            exchange_dir=exchange_dir,
            device_identity_path=device_identity_path,
            ledger_path=ledger_path,
        ),
    }


def _snapshot_records(
    exchange_dir: Path,
    *,
    session_id: str,
    source_device_id: str | None = None,
) -> list[dict[str, Any]]:
    return collect_snapshot_records(
        exchange_dir,
        entity_id=session_id,
        entity_id_key="session_id",
        read_metadata=_read_device_snapshot_metadata,
        metadata_error=ValueError,
        source_device_id=source_device_id,
    )


def _latest_snapshot_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    return max(records, key=_snapshot_record_sort_key)


def _snapshot_record_sort_key(record: dict[str, Any]) -> tuple[float, str]:
    metadata = record["metadata"]
    exported_at = metadata.get("exported_at")
    exported_value = float(exported_at) if isinstance(exported_at, int | float) else 0
    return (exported_value, _string(metadata.get("fingerprint")) or "")


def _snapshot_payload(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    metadata = record["metadata"]
    return {
        "fingerprint": metadata.get("fingerprint"),
        "parent_fingerprint": metadata.get("parent_fingerprint"),
        "source_device_id": metadata.get("source_device_id"),
        "source_device_name": metadata.get("source_device_name"),
        "snapshot_path": str(record["path"]),
        "root_size": metadata.get("root_size"),
        "exported_at": metadata.get("exported_at"),
        "title": _string(metadata.get("name")) or metadata.get("session_id"),
        "cwd": _string(metadata.get("cwd")),
        "latest_assistant_message": _string(metadata.get("latest_assistant_message")),
    }


def _local_session_payload(
    snapshot: ClaudeSessionSnapshot | None,
    fingerprint: str | None,
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "fingerprint": fingerprint,
        "updated_at_ms": snapshot.updated_at_ms,
        "title": snapshot.name,
        "cwd": snapshot.cwd,
        "latest_assistant_message": snapshot.latest_assistant_message,
        "root_path": str(snapshot.root_path),
    }


def _find_local_session_root(home: Path, session_id: str) -> Path | None:
    return next(home.glob(f"projects/*/{session_id}.jsonl"), None)


def _optional_fingerprint_id(snapshot: ClaudeSessionSnapshot | None) -> str | None:
    if snapshot is None:
        return None
    return _string(snapshot.fingerprint.get("tree_sha256"))


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


def _discover_sessions(
    home: Path,
    *,
    stability_delay_seconds: float,
) -> dict[str, ClaudeSessionSnapshot]:
    sessions: dict[str, ClaudeSessionSnapshot] = {}
    projects = home / "projects"
    if not projects.exists():
        return sessions
    for root in projects.glob("*/*.jsonl"):
        snapshot = _read_session_snapshot(
            home,
            root,
            stability_delay_seconds=stability_delay_seconds,
        )
        if snapshot is not None:
            sessions[snapshot.session_id] = snapshot
    return sessions


def _read_session_snapshot(
    home: Path,
    root: Path | None,
    *,
    stability_delay_seconds: float,
) -> ClaudeSessionSnapshot | None:
    if (
        root is None
        or root.is_symlink()
        or not root.is_file()
        or root.suffix != ".jsonl"
    ):
        return None
    if _rollout_open_for_write(root) or not path_stable(root, stability_delay_seconds):
        return None
    session_id = root.stem
    parsed = _parse_claude_jsonl(root, session_id)
    if parsed is None:
        return None
    fingerprint = _session_fingerprint(home, root)
    if fingerprint is None:
        return None
    project_key = root.parent.name
    cwd = parsed["cwd"] or _decode_project_key(project_key)
    fallback_name = Path(cwd).name if cwd else session_id
    return ClaudeSessionSnapshot(
        session_id=session_id,
        project_key=project_key,
        root_path=root,
        relative_root=root.relative_to(home),
        cwd=cwd,
        name=parsed["name"] or fallback_name or session_id,
        latest_assistant_message=parsed["latest_assistant_message"],
        created_at_ms=parsed["created_at_ms"],
        updated_at_ms=parsed["updated_at_ms"] or _mtime_ms(root),
        fingerprint=fingerprint,
    )


def _parse_claude_jsonl(root: Path, session_id: str) -> dict[str, Any] | None:
    seen_event = False
    seen_matching_session = False
    first_user: str | None = None
    latest_assistant: str | None = None
    first_timestamp_ms: int | None = None
    latest_timestamp_ms: int | None = None
    cwd: str | None = None

    try:
        lines = root.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        event_type = _string(payload.get("type"))
        if event_type in CLAUDE_EVENT_TYPES:
            seen_event = True
        payload_session_id = _string(payload.get("sessionId"))
        if payload_session_id == session_id:
            seen_matching_session = True
        cwd = cwd or _string(payload.get("cwd"))
        if timestamp_ms := _timestamp_ms(_string(payload.get("timestamp"))):
            first_timestamp_ms = min(first_timestamp_ms or timestamp_ms, timestamp_ms)
            latest_timestamp_ms = max(latest_timestamp_ms or 0, timestamp_ms)
        role = (
            _string((payload.get("message") or {}).get("role"))
            if isinstance(payload.get("message"), dict)
            else None
        )
        text = _message_text(payload.get("message"))
        if role == "user" and text and first_user is None:
            # Claude Code transcripts often open with harness-injected markup
            # (<local-command-caveat>, <command-name>, <system-reminder>);
            # skip it so session names come from real user text.
            first_user = _meaningful_user_text(text)
        elif role == "assistant" and text:
            latest_assistant = text
    if not seen_event:
        return None
    if session_id and not seen_matching_session:
        session_ids = {
            _string(json.loads(line).get("sessionId"))
            for line in lines
            if line.strip() and line.lstrip().startswith("{")
        }
        if any(value for value in session_ids):
            return None
    return {
        "cwd": cwd,
        "name": _preview(first_user),
        "latest_assistant_message": _preview(latest_assistant),
        "created_at_ms": first_timestamp_ms,
        "updated_at_ms": latest_timestamp_ms,
    }


_HARNESS_MARKUP_TAGS = (
    "system-reminder",
    "local-command-caveat",
    "command-name",
    "command-message",
    "command-args",
    "command-contents",
    "local-command-stdout",
)
_HARNESS_MARKUP_SPAN_RE = re.compile(
    r"<(" + "|".join(_HARNESS_MARKUP_TAGS) + r")>.*?</\1>",
    re.DOTALL,
)
_HARNESS_MARKUP_TAG_RE = re.compile(r"</?(" + "|".join(_HARNESS_MARKUP_TAGS) + r")>")


def _meaningful_user_text(text: str) -> str | None:
    """Return user-authored text with harness-injected markup removed."""
    cleaned = _HARNESS_MARKUP_SPAN_RE.sub(" ", text)
    cleaned = _HARNESS_MARKUP_TAG_RE.sub(" ", cleaned)
    cleaned = cleaned.strip()
    return cleaned or None


def _message_text(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts) if parts else None


def _session_fingerprint(home: Path, root: Path) -> dict[str, Any] | None:
    paths = _session_paths_for_root(home, root)
    digest = hashlib.sha256()
    root_digest = hashlib.sha256()
    try:
        with root.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                root_digest.update(chunk)
        for path in paths:
            if path.is_symlink():
                continue
            if path.is_dir():
                continue
            relative = path.relative_to(home).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    except OSError:
        return None
    stat = root.stat()
    return {
        "root_sha256": root_digest.hexdigest(),
        "root_size": stat.st_size,
        "tree_sha256": digest.hexdigest(),
        "updated_at_ms": _mtime_ms(root),
    }


def _session_paths(snapshot: ClaudeSessionSnapshot, home: Path) -> list[Path]:
    return _session_paths_for_root(home, snapshot.root_path)


def _session_paths_for_root(home: Path, root: Path) -> list[Path]:
    session_id = root.stem
    paths = [root]
    project_session_dir = root.parent / session_id
    for candidate in (
        project_session_dir,
        home / "session-env" / session_id,
        home / "tasks" / session_id,
        home / "file-history" / session_id,
    ):
        if candidate.exists():
            paths.extend(_walk_copyable_paths(candidate))
    return sorted(dict.fromkeys(paths), key=lambda item: item.as_posix())


def _walk_copyable_paths(root: Path) -> list[Path]:
    if root.is_symlink():
        return []
    if root.is_file():
        return [] if root.name == ".lock" else [root]
    paths = [root]
    for path in root.rglob("*"):
        if path.name == ".lock" or path.is_symlink():
            continue
        paths.append(path)
    return paths


def _copy_path(source: Path, target: Path, *, overwrite: bool) -> None:
    if source.is_symlink():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        if target.exists() and overwrite:
            shutil.rmtree(target)
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
        return
    if target.exists() and not overwrite:
        return
    shutil.copy2(source, target)


def _copy_session_into_home(
    *,
    source_home: Path,
    source_root: Path,
    target_home: Path,
    overwrite: bool,
) -> None:
    root_relative_path = source_root.relative_to(source_home)
    session_id = source_root.stem
    stage_root = target_home / IMPORT_STAGING_DIR_NAME / f"{session_id}-{uuid.uuid4()}"
    staged_files = stage_root / "files"
    try:
        _stage_session_files(
            source_home=source_home,
            source_root=source_root,
            staged_files=staged_files,
        )
        staged_snapshot = _read_session_snapshot(
            staged_files,
            staged_files / root_relative_path,
            stability_delay_seconds=0,
        )
        if staged_snapshot is None:
            raise ValueError(f"Staged Claude session is invalid: {session_id}")
        _commit_staged_session(
            staged_files=staged_files,
            target_home=target_home,
            root_relative_path=root_relative_path,
            overwrite=overwrite,
        )
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        _remove_empty_parent(stage_root.parent)
        raise
    shutil.rmtree(stage_root, ignore_errors=True)
    _remove_empty_parent(stage_root.parent)


def _stage_session_files(
    *,
    source_home: Path,
    source_root: Path,
    staged_files: Path,
) -> None:
    for source_path in _session_paths_for_root(source_home, source_root):
        if source_path.is_symlink():
            continue
        relative = source_path.relative_to(source_home)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        target_path = staged_files / relative
        _copy_path(source_path, target_path, overwrite=True)


def _commit_staged_session(
    *,
    staged_files: Path,
    target_home: Path,
    root_relative_path: Path,
    overwrite: bool,
) -> None:
    commit_relatives = _staged_session_commit_relatives(
        staged_files, root_relative_path
    )
    backup_root = (
        target_home
        / IMPORT_BACKUP_DIR_NAME
        / f"{root_relative_path.stem}-{uuid.uuid4()}"
    )
    moved_targets: list[tuple[Path, Path]] = []
    moved_backups: list[tuple[Path, Path]] = []
    try:
        for relative in commit_relatives:
            source_path = staged_files / relative
            if not source_path.exists():
                continue
            target_path = target_home / relative
            if target_path.exists() or target_path.is_symlink():
                if not overwrite:
                    continue
                backup_path = backup_root / "files" / relative
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target_path), str(backup_path))
                moved_backups.append((target_path, backup_path))
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_path), str(target_path))
            moved_targets.append((target_path, relative))
    except Exception:
        _restore_failed_session_commit(
            moved_targets=moved_targets,
            moved_backups=moved_backups,
            failed_root=backup_root / "failed",
        )
        raise
    shutil.rmtree(backup_root, ignore_errors=True)


def _staged_session_commit_relatives(
    staged_files: Path,
    root_relative_path: Path,
) -> list[Path]:
    session_id = root_relative_path.stem
    candidates = [
        root_relative_path.parent / session_id,
        Path("session-env") / session_id,
        Path("tasks") / session_id,
        Path("file-history") / session_id,
    ]
    existing = [
        relative for relative in candidates if (staged_files / relative).exists()
    ]
    existing.append(root_relative_path)
    return existing


def _restore_failed_session_commit(
    *,
    moved_targets: list[tuple[Path, Path]],
    moved_backups: list[tuple[Path, Path]],
    failed_root: Path,
) -> None:
    for target_path, relative in reversed(moved_targets):
        if not target_path.exists() and not target_path.is_symlink():
            continue
        failed_path = failed_root / relative
        failed_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target_path), str(failed_path))
    for target_path, backup_path in reversed(moved_backups):
        if not backup_path.exists() or target_path.exists() or target_path.is_symlink():
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup_path), str(target_path))


_remove_empty_parent = remove_empty_dir


def _write_device_snapshot(
    *,
    exchange_dir: Path,
    identity: DeviceIdentity,
    openbase_home: Path,
    snapshot: ClaudeSessionSnapshot,
    fingerprint_id: str,
    parent_fingerprint: str | None,
    source_user_home: Path,
) -> Path:
    target_dir = (
        exchange_dir
        / "devices"
        / identity.device_id
        / "snapshots"
        / snapshot.session_id
        / fingerprint_id
    )
    if target_dir.exists():
        return target_dir
    tmp_dir = target_dir.parent / f".tmp-{fingerprint_id}-{uuid.uuid4()}"
    files_dir = tmp_dir / "files"
    tmp_dir.mkdir(parents=True, exist_ok=False)
    try:
        copied_files: list[str] = []
        for source_path in _session_paths(snapshot, openbase_home):
            relative = source_path.relative_to(openbase_home)
            target_path = files_dir / relative
            if source_path.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            _copy_path(source_path, target_path, overwrite=True)
            copied_files.append(relative.as_posix())
        metadata = _device_snapshot_metadata(
            identity=identity,
            snapshot=snapshot,
            fingerprint_id=fingerprint_id,
            parent_fingerprint=parent_fingerprint,
            copied_files=copied_files,
            source_user_home=source_user_home,
        )
        (tmp_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_dir, target_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return target_dir


def _device_snapshot_metadata(
    *,
    identity: DeviceIdentity,
    snapshot: ClaudeSessionSnapshot,
    fingerprint_id: str,
    parent_fingerprint: str | None,
    copied_files: list[str],
    source_user_home: Path,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_device_id": identity.device_id,
        "source_device_name": identity.device_name,
        "source_user_home": str(source_user_home),
        "session_id": snapshot.session_id,
        "fingerprint": fingerprint_id,
        "parent_fingerprint": parent_fingerprint,
        "exported_at": time.time(),
        "root_relative_path": snapshot.relative_root.as_posix(),
        "project_key": snapshot.project_key,
        "cwd": snapshot.cwd,
        "name": snapshot.name,
        "latest_assistant_message": snapshot.latest_assistant_message,
        "root_sha256": snapshot.fingerprint["root_sha256"],
        "root_size": snapshot.fingerprint["root_size"],
        "tree_sha256": snapshot.fingerprint["tree_sha256"],
        "files": copied_files,
    }


def _read_device_snapshot_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError("metadata_not_found")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("metadata_malformed") from exc
    if not isinstance(metadata, dict):
        raise ValueError("metadata_malformed")
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported_schema")
    for key in (
        "source_device_id",
        "session_id",
        "fingerprint",
        "root_relative_path",
        "root_sha256",
        "tree_sha256",
    ):
        if not _string(metadata.get(key)):
            raise ValueError(f"metadata_missing_{key}")
    root_relative_path = Path(str(metadata["root_relative_path"]))
    if root_relative_path.is_absolute() or ".." in root_relative_path.parts:
        raise ValueError("metadata_invalid_root_relative_path")
    return metadata


def _validate_device_snapshot(
    snapshot_dir: Path, metadata: dict[str, Any]
) -> str | None:
    files_dir = snapshot_dir / "files"
    root_relative_path = Path(str(metadata["root_relative_path"]))
    snapshot = _read_session_snapshot(
        files_dir,
        files_dir / root_relative_path,
        stability_delay_seconds=0,
    )
    if snapshot is None:
        return "session_not_found"
    if snapshot.fingerprint["root_sha256"] != metadata.get("root_sha256"):
        return "root_hash_mismatch"
    if snapshot.fingerprint["root_size"] != metadata.get("root_size"):
        return "root_size_mismatch"
    if snapshot.fingerprint["tree_sha256"] != metadata.get("tree_sha256"):
        return "tree_hash_mismatch"
    return None


def _import_device_snapshot_into_home(
    *,
    snapshot_dir: Path,
    metadata: dict[str, Any],
    openbase_home: Path,
    overwrite: bool,
) -> None:
    files_dir = snapshot_dir / "files"
    root_relative_path = Path(str(metadata["root_relative_path"]))
    session_id = str(metadata["session_id"])
    source_root = files_dir / root_relative_path
    _copy_session_into_home(
        source_home=files_dir,
        source_root=source_root,
        target_home=openbase_home,
        overwrite=overwrite,
    )
    if not (openbase_home / root_relative_path).exists():
        raise FileNotFoundError(f"Imported Claude session root missing: {session_id}")


def _fingerprint_id(fingerprint: dict[str, Any] | None) -> str:
    value = _string(fingerprint.get("tree_sha256")) if fingerprint else None
    if not value:
        raise ValueError("fingerprint_missing_tree_sha256")
    return value


def _target_root_path(source_root: Path, source_home: Path, target_home: Path) -> Path:
    return target_home / source_root.relative_to(source_home)


def _translated_metadata_cwd(
    metadata: dict[str, Any], target_user_home: Path
) -> str | None:
    source_value = _string(metadata.get("source_user_home"))
    source_home = Path(source_value) if source_value else None
    if source_home is not None and not source_home.is_absolute():
        source_home = None
    return translate_home_path(
        _string(metadata.get("cwd")),
        source_home=source_home,
        target_home=target_user_home,
    )


def _translate_super_agent_session_cwds(
    db_path: Path | None, target_user_home: Path
) -> None:
    """Migrate foreign-home cwd values retained by older thread imports."""
    resolved_db = db_path or _super_agents_db_path()
    if not resolved_db.is_file():
        return
    with sqlite3.connect(resolved_db) as conn:
        conn.row_factory = sqlite3.Row
        has_sessions = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        ).fetchone()
        if has_sessions is None:
            return
        rows = conn.execute("SELECT id, cwd FROM sessions").fetchall()
        for row in rows:
            translated = translate_home_path(row["cwd"], target_home=target_user_home)
            if translated and translated != row["cwd"]:
                conn.execute(
                    "UPDATE sessions SET cwd = ? WHERE id = ?",
                    (translated, row["id"]),
                )
        conn.commit()


def _backfill_openbase_session_metadata(
    snapshot: ClaudeSessionSnapshot,
    *,
    db_path: Path | None,
    cwd_override: str | None = None,
) -> None:
    resolved_db = db_path or _super_agents_db_path()
    resolved_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(resolved_db) as conn:
        _ensure_super_agents_schema(conn)
        existing = conn.execute(
            "select * from sessions where backend_session_id = ?",
            (snapshot.session_id,),
        ).fetchone()
        if existing is None:
            session_id = f"claude_{snapshot.session_id.replace('-', '')}"
            name = _unique_session_name(conn, snapshot.name)
            created_at = _iso_from_ms(snapshot.created_at_ms) or _iso_now()
            updated_at = _iso_from_ms(snapshot.updated_at_ms) or created_at
            conn.execute(
                """
                insert into sessions (
                    id, name, cwd, command_json, status, last_observed_state,
                    last_useful_message, backend_session_id, log_path,
                    raw_log_path, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    name,
                    cwd_override or snapshot.cwd or str(Path.home()),
                    json.dumps(["claude", "--resume", snapshot.session_id]),
                    # Idle synced sessions are "completed"; "waiting" means
                    # blocked on user input and counts as active.
                    "completed",
                    _session_observed_state(snapshot),
                    snapshot.latest_assistant_message,
                    snapshot.session_id,
                    None,
                    None,
                    created_at,
                    updated_at,
                ),
            )
            return
        updates: dict[str, Any] = {}
        if (
            snapshot.latest_assistant_message
            and existing["last_useful_message"] != snapshot.latest_assistant_message
        ):
            updates["last_useful_message"] = snapshot.latest_assistant_message
        if (
            _string(existing["name"])
            and _string(existing["name"]).startswith("<")
            and snapshot.name
            and not snapshot.name.startswith("<")
        ):
            # Self-heal names imported before harness markup was stripped.
            updates["name"] = _unique_session_name(conn, snapshot.name)
        desired_cwd = cwd_override or snapshot.cwd
        if desired_cwd and existing["cwd"] != desired_cwd:
            updates["cwd"] = desired_cwd
        if _should_refresh_observed_state(existing["last_observed_state"]):
            updates["last_observed_state"] = _session_observed_state(snapshot)
        if not updates:
            return
        updates["updated_at"] = _iso_from_ms(snapshot.updated_at_ms) or _iso_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"update sessions set {assignments} where id = ?",
            [*updates.values(), existing["id"]],
        )


def _ensure_super_agents_schema(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        create table if not exists sessions (
            id text primary key,
            name text not null unique,
            agent_name text,
            developer_instructions text,
            cwd text not null,
            command_json text not null,
            model text,
            status text not null,
            pid integer,
            active_turn_id text,
            last_turn_id text,
            last_observed_state text,
            last_useful_message text,
            backend_session_id text,
            last_exit_code integer,
            log_path text,
            raw_log_path text,
            created_at text not null,
            updated_at text not null
        );
        create table if not exists turns (
            id text primary key,
            session_id text not null references sessions(id) on delete cascade,
            prompt text not null,
            mode text,
            model text,
            reasoning_effort text,
            status text not null,
            attempts integer not null default 0,
            last_error text,
            last_useful_message text,
            created_at text not null,
            updated_at text not null,
            finished_at text
        );
        create index if not exists turns_session_idx on turns(session_id, created_at);
        """
    )
    session_columns = {
        row["name"] for row in conn.execute("pragma table_info(sessions)").fetchall()
    }
    session_defaults = {
        "agent_name": "text",
        "developer_instructions": "text",
        "model": "text",
        "pid": "integer",
        "active_turn_id": "text",
        "last_turn_id": "text",
        "last_observed_state": "text",
        "last_useful_message": "text",
        "backend_session_id": "text",
        "last_exit_code": "integer",
        "log_path": "text",
        "raw_log_path": "text",
    }
    for column, column_type in session_defaults.items():
        if column not in session_columns:
            conn.execute(f"alter table sessions add column {column} {column_type}")
    turn_columns = {
        row["name"] for row in conn.execute("pragma table_info(turns)").fetchall()
    }
    turn_defaults = {
        "mode": "text",
        "model": "text",
        "reasoning_effort": "text",
        "attempts": "integer not null default 0",
        "last_error": "text",
        "last_useful_message": "text",
        "finished_at": "text",
    }
    for column, column_type in turn_defaults.items():
        if column not in turn_columns:
            conn.execute(f"alter table turns add column {column} {column_type}")


def _active_claude_session_ids(db_path: Path | None = None) -> set[str]:
    active: set[str] = set()
    resolved_db = db_path or _super_agents_db_path()
    if resolved_db.exists():
        with sqlite3.connect(resolved_db) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    select backend_session_id from sessions
                    where backend_session_id is not null
                    and (status = 'running' or active_turn_id is not null)
                    """
                ).fetchall()
            except sqlite3.Error:
                rows = []
        active.update(
            row["backend_session_id"] for row in rows if row["backend_session_id"]
        )
    active.update(_active_claude_session_ids_from_legacy_state())
    return active


def _active_claude_session_ids_from_legacy_state(
    state_path: Path = DEFAULT_LEGACY_SUPER_AGENTS_STATE_PATH,
) -> set[str]:
    if not state_path.exists():
        return set()
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    if not isinstance(sessions, dict):
        return set()
    active: set[str] = set()
    for value in sessions.values():
        if not isinstance(value, dict):
            continue
        status = value.get("status") or value.get("lastStatus")
        active_turn = value.get("activeTurnId") or value.get("active_turn_id")
        backend_session_id = _string(
            value.get("backendSessionId") or value.get("backend_session_id")
        )
        if backend_session_id and (status == "running" or active_turn):
            active.add(backend_session_id)
    return active


def _super_agents_db_path() -> Path:
    return super_agents_state_db_path()


def _unique_session_name(conn: sqlite3.Connection, base_name: str) -> str:
    name = _preview(base_name, limit=80) or "Claude Code session"
    candidate = name
    suffix = 2
    while conn.execute(
        "select 1 from sessions where name = ?", (candidate,)
    ).fetchone():
        suffix_text = f" ({suffix})"
        candidate = f"{name[: 80 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return candidate


def _session_observed_state(snapshot: ClaudeSessionSnapshot) -> str:
    payload = {
        "source": "claude_thread_sync",
        "backend": "claude_code",
        "backend_session_id": snapshot.session_id,
        "project_key": snapshot.project_key,
        "root_relative_path": snapshot.relative_root.as_posix(),
        "cwd": snapshot.cwd,
        "root_sha256": snapshot.fingerprint.get("root_sha256"),
        "tree_sha256": snapshot.fingerprint.get("tree_sha256"),
        "created_at": _iso_from_ms(snapshot.created_at_ms),
        "updated_at": _iso_from_ms(snapshot.updated_at_ms),
        "observed_at": _iso_now(),
    }
    return json.dumps(payload, sort_keys=True)


def _should_refresh_observed_state(value: str | None) -> bool:
    if not value:
        return True
    if value == "Claude Code session imported by thread sync":
        return True
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(raw, dict) and raw.get("source") == "claude_thread_sync"


def _read_sync_ledger(path: Path) -> dict[str, Any]:
    return read_scoped_ledger(
        path,
        scope_key="sessions",
        logger=logger,
        malformed_event="claude_thread_sync event=ledger_malformed",
    )


def _write_sync_ledger(path: Path, ledger: dict[str, Any]) -> None:
    write_scoped_ledger(path, scope_key="sessions", ledger=ledger)


def _read_device_ledger(path: Path) -> dict[str, Any]:
    return read_device_ledger(
        path,
        scope_key="sessions",
        logger=logger,
        malformed_event="claude_thread_device_sync event=ledger_malformed",
    )


def _write_device_ledger(path: Path, ledger: dict[str, Any]) -> None:
    write_json_atomic(path, ledger)


def _record_synced_pair(
    ledger: dict[str, Any],
    session_id: str,
    normal_fingerprint: dict[str, Any],
    openbase_fingerprint: dict[str, Any],
    reason: str,
) -> None:
    record_synced_pair(
        ledger,
        entity_key="session_id",
        entity_id=session_id,
        left_key="normal",
        left_fingerprint=normal_fingerprint,
        right_key="openbase",
        right_fingerprint=openbase_fingerprint,
        reason=reason,
    )


def _record_conflict(
    ledger: dict[str, Any],
    session_id: str,
    normal_fingerprint: dict[str, Any],
    openbase_fingerprint: dict[str, Any],
    reason: str,
) -> None:
    record_sync_conflict(
        ledger,
        entity_key="session_id",
        entity_id=session_id,
        left_key="normal",
        left_fingerprint=normal_fingerprint,
        right_key="openbase",
        right_fingerprint=openbase_fingerprint,
        reason=reason,
    )


def _same_fingerprint(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left.get("tree_sha256") == right.get("tree_sha256")


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


def _timestamp_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def _mtime_ms(path: Path) -> int:
    return int(path.stat().st_mtime * 1000)


def _preview(text: str | None, limit: int = 180) -> str | None:
    if text is None:
        return None
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."


def _decode_project_key(value: str) -> str | None:
    if not value.startswith("-"):
        return None
    return "/" + value[1:].replace("-", "/")


def _iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return (
        datetime.fromtimestamp(value / 1000, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
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
