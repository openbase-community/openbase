"""Cross-device snapshot sync for Claude Code sessions in the shared ~/.claude."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from openbase_coder_cli.paths import CLAUDE_CONFIG_DIR

from .claude_conflict_payloads import (
    claude_thread_snapshot_conflicts_payload,
    resolve_claude_snapshot_conflict,
)
from .claude_jsonl import _meaningful_user_text
from .claude_ledger import (
    _read_device_ledger,
    _write_device_ledger,
)
from .claude_models import (
    DEFAULT_DEVICE_EXCHANGE_DIR,
    DEFAULT_DEVICE_LEDGER_PATH,
    DEFAULT_SYNC_MAX_AGE_DAYS,
    ClaudeConflictResolutionError,
    ClaudeSessionSnapshot,
    ClaudeThreadSnapshotResult,
)
from .claude_session_db import (
    _active_claude_session_ids,
    _backfill_openbase_session_metadata,
    _translate_super_agent_session_cwds,
    _translated_metadata_cwd,
)
from .claude_snapshot_io import (
    _discover_sessions,
    _fingerprint_id,
    _import_device_snapshot_into_home,
    _read_device_snapshot_metadata,
    _read_session_snapshot,
    _validate_device_snapshot,
    _write_device_snapshot,
)
from .thread_exchange import DEFAULT_DEVICE_IDENTITY_PATH
from .thread_sync_common import (
    DeviceIdentity,
    LocalSnapshotState,
    SnapshotExportCandidate,
    SnapshotImportSource,
    device_snapshot_dirs,
    file_content_relation,
    get_or_create_device_identity,
    read_device_identity,
    run_snapshot_export,
    run_snapshot_import,
    sync_cutoff_ms,
)

__all__ = [
    "ClaudeConflictResolutionError",
    "ClaudeSessionSnapshot",
    "ClaudeThreadSnapshotResult",
    "DEFAULT_DEVICE_EXCHANGE_DIR",
    "DEFAULT_DEVICE_LEDGER_PATH",
    "DEFAULT_SYNC_MAX_AGE_DAYS",
    "DEFAULT_DEVICE_IDENTITY_PATH",
    "claude_thread_snapshot_conflicts_payload",
    "claude_thread_snapshot_status",
    "export_claude_thread_snapshots",
    "import_claude_thread_snapshots",
    "resolve_claude_snapshot_conflict",
    "sync_claude_thread_snapshots_once",
    "_meaningful_user_text",
]

logger = logging.getLogger(__name__)


def sync_claude_thread_snapshots_once(
    *,
    claude_home: Path = CLAUDE_CONFIG_DIR,
    exchange_dir: Path = DEFAULT_DEVICE_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_DEVICE_LEDGER_PATH,
    super_agents_db_path: Path | None = None,
    stability_delay_seconds: float = 0.2,
    max_age_days: int | None = DEFAULT_SYNC_MAX_AGE_DAYS,
    user_home: Path | None = None,
) -> dict[str, list[ClaudeThreadSnapshotResult]]:
    exports = export_claude_thread_snapshots(
        claude_home=claude_home,
        exchange_dir=exchange_dir,
        device_identity_path=device_identity_path,
        ledger_path=ledger_path,
        super_agents_db_path=super_agents_db_path,
        stability_delay_seconds=stability_delay_seconds,
        max_age_days=max_age_days,
        source_user_home=user_home,
    )
    imports = import_claude_thread_snapshots(
        claude_home=claude_home,
        exchange_dir=exchange_dir,
        device_identity_path=device_identity_path,
        ledger_path=ledger_path,
        super_agents_db_path=super_agents_db_path,
        target_user_home=user_home,
    )
    return {"exports": exports, "imports": imports}


def export_claude_thread_snapshots(
    *,
    claude_home: Path = CLAUDE_CONFIG_DIR,
    exchange_dir: Path = DEFAULT_DEVICE_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_DEVICE_LEDGER_PATH,
    super_agents_db_path: Path | None = None,
    stability_delay_seconds: float = 0.2,
    max_age_days: int | None = DEFAULT_SYNC_MAX_AGE_DAYS,
    active_session_ids: set[str] | None = None,
    source_user_home: Path | None = None,
) -> list[ClaudeThreadSnapshotResult]:
    claude_home = claude_home.expanduser()
    claude_home.mkdir(parents=True, exist_ok=True)
    identity = get_or_create_device_identity(device_identity_path)
    active_ids = set(active_session_ids or set()) | _active_claude_session_ids(
        super_agents_db_path
    )
    ledger = _read_device_ledger(ledger_path)
    sessions = _discover_sessions(
        claude_home,
        stability_delay_seconds=stability_delay_seconds,
    )
    results = run_snapshot_export(
        candidates=_export_candidates(
            sessions,
            exchange_dir=exchange_dir,
            identity=identity,
            claude_home=claude_home,
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
    claude_home: Path,
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
                claude_home=claude_home,
                snapshot=snapshot,
                fingerprint_id=fingerprint_id,
                source_user_home=source_user_home,
            ),
        )


def _device_snapshot_writer(
    *,
    exchange_dir: Path,
    identity: DeviceIdentity,
    claude_home: Path,
    snapshot: ClaudeSessionSnapshot,
    fingerprint_id: str,
    source_user_home: Path,
) -> Callable[[str | None], Path]:
    def write(parent_fingerprint: str | None) -> Path:
        return _write_device_snapshot(
            exchange_dir=exchange_dir,
            identity=identity,
            claude_home=claude_home,
            snapshot=snapshot,
            fingerprint_id=fingerprint_id,
            parent_fingerprint=parent_fingerprint,
            source_user_home=source_user_home,
        )

    return write


def import_claude_thread_snapshots(
    *,
    claude_home: Path = CLAUDE_CONFIG_DIR,
    exchange_dir: Path = DEFAULT_DEVICE_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_DEVICE_LEDGER_PATH,
    super_agents_db_path: Path | None = None,
    target_user_home: Path | None = None,
) -> list[ClaudeThreadSnapshotResult]:
    claude_home = claude_home.expanduser()
    claude_home.mkdir(parents=True, exist_ok=True)
    target_home = target_user_home or Path.home()
    _translate_super_agent_session_cwds(super_agents_db_path, target_home)
    identity = get_or_create_device_identity(device_identity_path)
    ledger = _read_device_ledger(ledger_path)
    results = run_snapshot_import(
        exchange_dir=exchange_dir,
        device_id=identity.device_id,
        ledger=ledger,
        source=_device_import_source(
            claude_home=claude_home,
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
    claude_home: Path,
    active_ids: set[str],
    super_agents_db_path: Path | None,
    target_user_home: Path,
) -> SnapshotImportSource:
    def load_local(metadata: dict[str, Any]) -> LocalSnapshotState:
        local_snapshot = _read_session_snapshot(
            claude_home,
            claude_home / Path(metadata["root_relative_path"]),
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
                claude_home=claude_home,
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
            claude_home,
            claude_home / Path(metadata["root_relative_path"]),
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
