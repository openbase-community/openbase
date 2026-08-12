"""Cross-device Codex thread snapshot exchange."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openbase_coder_cli.paths import (
    CODEX_HOME_DIR,
    NORMAL_CODEX_HOME_DIR,
    OPENBASE_BASE_DIR,
)

from .thread_import import (
    DEFAULT_SYNC_MAX_AGE_DAYS,
    SESSION_INDEX_NAME,
    STATE_DB_NAME,
    SYNC_LEDGER_NAME,
    ThreadTransferError,
    _active_super_agent_thread_ids,
    _connect,
    _fingerprint_from_rollout_path,
    _has_table,
    _latest_session_index_entries,
    _row_updated_ms,
    _source_rollout_path,
    _string,
    _table_columns,
    _target_row_safe_for_overwrite,
    _thread_fingerprint,
    _thread_rows,
    _thread_safe_for_sync,
)
from .thread_sync_common import (
    DeviceIdentity,
    LocalSnapshotState,
    SnapshotExportCandidate,
    SnapshotImportSource,
    collect_snapshot_records,
    device_snapshot_dirs,
    file_content_relation,
    find_snapshot_record,
    read_device_ledger,
    read_scoped_ledger,
    record_device_snapshot,
    remove_empty_dir,
    run_snapshot_export,
    run_snapshot_import,
    sync_cutoff_ms,
    translate_home_path,
    write_json_atomic,
)
from .thread_sync_common import (
    get_or_create_device_identity as _get_or_create_device_identity,
)
from .thread_sync_common import (
    read_device_identity as _read_device_identity,
)

SCHEMA_VERSION = 1
DEVICE_IDENTITY_NAME = "thread-sync-device.json"
LEDGER_NAME = "codex-thread-device-sync-ledger.json"
IMPORT_STAGING_DIR_NAME = ".codex-thread-sync-staging"
IMPORT_BACKUP_DIR_NAME = ".codex-thread-sync-backups"
DEFAULT_EXCHANGE_DIR = OPENBASE_BASE_DIR / "thread-sync"
DEFAULT_DEVICE_IDENTITY_PATH = OPENBASE_BASE_DIR / DEVICE_IDENTITY_NAME
DEFAULT_LEDGER_PATH = OPENBASE_BASE_DIR / LEDGER_NAME
DEFAULT_HOME_SYNC_LEDGER_PATH = OPENBASE_BASE_DIR / SYNC_LEDGER_NAME

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThreadSnapshotResult:
    thread_id: str
    status: str
    reason: str
    snapshot_path: str | None = None
    fingerprint: str | None = None
    source_device_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "status": self.status,
            "reason": self.reason,
            "snapshot_path": self.snapshot_path,
            "fingerprint": self.fingerprint,
            "source_device_id": self.source_device_id,
        }


class ThreadConflictResolutionError(ValueError):
    """Raised when a thread sync conflict cannot be resolved."""


def get_or_create_device_identity(
    path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
) -> DeviceIdentity:
    return _get_or_create_device_identity(path)


def read_device_identity(
    path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
) -> DeviceIdentity | None:
    return _read_device_identity(path)


def sync_thread_snapshots_once(
    *,
    codex_home: Path = CODEX_HOME_DIR,
    exchange_dir: Path = DEFAULT_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
    stability_delay_seconds: float = 0.2,
    max_age_days: int | None = DEFAULT_SYNC_MAX_AGE_DAYS,
    user_home: Path | None = None,
) -> dict[str, list[ThreadSnapshotResult]]:
    exports = export_thread_snapshots(
        codex_home=codex_home,
        exchange_dir=exchange_dir,
        device_identity_path=device_identity_path,
        ledger_path=ledger_path,
        stability_delay_seconds=stability_delay_seconds,
        max_age_days=max_age_days,
        source_user_home=user_home,
    )
    imports = import_thread_snapshots(
        codex_home=codex_home,
        exchange_dir=exchange_dir,
        device_identity_path=device_identity_path,
        ledger_path=ledger_path,
        target_user_home=user_home,
    )
    return {"exports": exports, "imports": imports}


def export_thread_snapshots(
    *,
    codex_home: Path = CODEX_HOME_DIR,
    exchange_dir: Path = DEFAULT_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
    stability_delay_seconds: float = 0.2,
    max_age_days: int | None = DEFAULT_SYNC_MAX_AGE_DAYS,
    active_thread_ids: set[str] | None = None,
    source_user_home: Path | None = None,
) -> list[ThreadSnapshotResult]:
    state_db = codex_home / STATE_DB_NAME
    if not state_db.exists():
        raise ThreadTransferError(f"Codex state database not found: {state_db}")

    identity = get_or_create_device_identity(device_identity_path)
    active_ids = set(active_thread_ids or set()) | _active_super_agent_thread_ids()
    ledger = _read_exchange_ledger(ledger_path)
    results = run_snapshot_export(
        candidates=_export_candidates(
            state_db=state_db,
            codex_home=codex_home,
            exchange_dir=exchange_dir,
            identity=identity,
            active_ids=active_ids,
            cutoff_ms=sync_cutoff_ms(max_age_days),
            index_entries=_latest_session_index_entries(
                codex_home / SESSION_INDEX_NAME
            ),
            stability_delay_seconds=stability_delay_seconds,
            source_user_home=source_user_home or Path.home(),
        ),
        device_id=identity.device_id,
        ledger=ledger,
        scope_key="threads",
        result_factory=ThreadSnapshotResult,
    )
    _write_exchange_ledger(ledger_path, ledger)
    return results


def _export_candidates(
    *,
    state_db: Path,
    codex_home: Path,
    exchange_dir: Path,
    identity: DeviceIdentity,
    active_ids: set[str],
    cutoff_ms: int | None,
    index_entries: dict[str, dict[str, Any]],
    stability_delay_seconds: float,
    source_user_home: Path,
) -> Iterator[SnapshotExportCandidate]:
    for row in _thread_rows(state_db):
        thread_id = _string(row.get("id"))
        if not thread_id:
            continue
        if cutoff_ms is not None and _row_updated_ms(row) < cutoff_ms:
            yield SnapshotExportCandidate(thread_id, skip_reason="skipped_old")
            continue
        if thread_id in active_ids:
            yield SnapshotExportCandidate(thread_id, skip_reason="skipped_active")
            continue

        safety = _thread_safe_for_sync(
            row,
            codex_home,
            thread_id,
            stability_delay_seconds=stability_delay_seconds,
        )
        if not safety.safe:
            yield SnapshotExportCandidate(thread_id, skip_reason=safety.reason)
            continue

        rollout = _source_rollout_path(row, codex_home, thread_id)
        fingerprint = _fingerprint_from_rollout_path(rollout, row)
        if rollout is None or fingerprint is None:
            yield SnapshotExportCandidate(thread_id, skip_reason="rollout_not_found")
            continue
        fingerprint_id = _fingerprint_id(fingerprint)
        yield SnapshotExportCandidate(
            thread_id,
            fingerprint_id=fingerprint_id,
            write_snapshot=_snapshot_writer(
                exchange_dir=exchange_dir,
                identity=identity,
                codex_home=codex_home,
                state_db=state_db,
                row=row,
                rollout=rollout,
                fingerprint=fingerprint,
                fingerprint_id=fingerprint_id,
                index_entry=index_entries.get(thread_id),
                source_user_home=source_user_home,
            ),
        )


def _snapshot_writer(
    *,
    exchange_dir: Path,
    identity: DeviceIdentity,
    codex_home: Path,
    state_db: Path,
    row: dict[str, Any],
    rollout: Path,
    fingerprint: dict[str, Any],
    fingerprint_id: str,
    index_entry: dict[str, Any] | None,
    source_user_home: Path,
) -> Callable[[str | None], Path]:
    def write(parent_fingerprint: str | None) -> Path:
        return _write_snapshot(
            exchange_dir=exchange_dir,
            identity=identity,
            codex_home=codex_home,
            row=row,
            rollout=rollout,
            fingerprint=fingerprint,
            fingerprint_id=fingerprint_id,
            parent_fingerprint=parent_fingerprint,
            index_entry=index_entry,
            dynamic_tools=_thread_dynamic_tools(state_db, row["id"]),
            source_user_home=source_user_home,
        )

    return write


def import_thread_snapshots(
    *,
    codex_home: Path = CODEX_HOME_DIR,
    exchange_dir: Path = DEFAULT_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
    target_user_home: Path | None = None,
) -> list[ThreadSnapshotResult]:
    state_db = codex_home / STATE_DB_NAME
    if not state_db.exists():
        raise ThreadTransferError(f"Codex state database not found: {state_db}")

    target_home = target_user_home or Path.home()
    _translate_existing_thread_cwds(state_db, target_home)
    identity = get_or_create_device_identity(device_identity_path)
    ledger = _read_exchange_ledger(ledger_path)
    results = run_snapshot_import(
        exchange_dir=exchange_dir,
        device_id=identity.device_id,
        ledger=ledger,
        source=_exchange_import_source(
            state_db=state_db,
            codex_home=codex_home,
            target_user_home=target_home,
        ),
        result_factory=ThreadSnapshotResult,
    )
    _write_exchange_ledger(ledger_path, ledger)
    return results


def _exchange_import_source(
    *,
    state_db: Path,
    codex_home: Path,
    target_user_home: Path,
) -> SnapshotImportSource:
    def load_local(metadata: dict[str, Any]) -> LocalSnapshotState:
        thread_id = metadata["thread_id"]
        local_row = _exchange_thread_row(state_db, thread_id)
        local_fingerprint = (
            _fingerprint_id(_thread_fingerprint(local_row, codex_home, thread_id))
            if local_row is not None
            else None
        )
        return LocalSnapshotState(local_row is not None, local_fingerprint, local_row)

    def import_blocked_reason(
        metadata: dict[str, Any], local: LocalSnapshotState
    ) -> str | None:
        if local.context is not None and not _target_row_safe_for_overwrite(
            local.context,
            codex_home,
            metadata["thread_id"],
        ):
            return "target_active"
        return None

    def perform_import(
        snapshot_dir: Path, metadata: dict[str, Any], local: LocalSnapshotState
    ) -> str | None:
        _import_snapshot_into_home(
            snapshot_dir=snapshot_dir,
            metadata=metadata,
            codex_home=codex_home,
            overwrite=local.exists,
            target_user_home=target_user_home,
        )
        return None

    def compare_content(
        snapshot_dir: Path, metadata: dict[str, Any], local: LocalSnapshotState
    ) -> str | None:
        if not local.exists or not isinstance(local.context, dict):
            return None
        rollout = _source_rollout_path(local.context, codex_home, metadata["thread_id"])
        if rollout is None:
            return None
        return file_content_relation(rollout, snapshot_dir / "rollout.jsonl")

    return SnapshotImportSource(
        scope_key="threads",
        entity_id_key="thread_id",
        read_metadata=_read_snapshot_metadata,
        metadata_error=ThreadTransferError,
        validate_snapshot=_validate_snapshot,
        load_local=load_local,
        import_blocked_reason=import_blocked_reason,
        perform_import=perform_import,
        compare_content=compare_content,
    )


def thread_snapshot_status(
    *,
    exchange_dir: Path = DEFAULT_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
) -> dict[str, Any]:
    identity = read_device_identity(device_identity_path)
    ledger = _read_exchange_ledger(ledger_path)
    conflicts = [
        {"thread_id": thread_id, **value["conflict"]}
        for thread_id, value in ledger.get("threads", {}).items()
        if isinstance(value, dict) and isinstance(value.get("conflict"), dict)
    ]
    snapshots = list(device_snapshot_dirs(exchange_dir))
    return {
        "device": identity.to_json() if identity else None,
        "exchange_dir": str(exchange_dir),
        "ledger_path": str(ledger_path),
        "snapshot_count": len(snapshots),
        "thread_count": len(ledger.get("threads", {})),
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
    }


def thread_snapshot_conflicts_payload(
    *,
    codex_home: Path = CODEX_HOME_DIR,
    exchange_dir: Path = DEFAULT_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
) -> dict[str, Any]:
    identity = read_device_identity(device_identity_path)
    ledger = _read_exchange_ledger(ledger_path)
    conflicts: list[dict[str, Any]] = []
    state_db = codex_home / STATE_DB_NAME
    for thread_id, thread_ledger in ledger.get("threads", {}).items():
        if not isinstance(thread_id, str) or not isinstance(thread_ledger, dict):
            continue
        conflict = thread_ledger.get("conflict")
        if not isinstance(conflict, dict):
            continue
        source_device_id = _string(conflict.get("source_device_id"))
        snapshots = _snapshot_records(
            exchange_dir,
            thread_id=thread_id,
            source_device_id=source_device_id,
        )
        incoming_snapshot = _snapshot_payload(
            _find_snapshot_record(
                snapshots,
                _string(conflict.get("incoming_fingerprint")),
            )
        )
        latest_remote = _snapshot_payload(_latest_snapshot_record(snapshots))
        local_row = _exchange_thread_row(state_db, thread_id)
        local_fingerprint = _fingerprint_id(
            _thread_fingerprint(local_row, codex_home, thread_id)
        )
        title = (
            _string((latest_remote or {}).get("title"))
            or _string((incoming_snapshot or {}).get("title"))
            or _string((local_row or {}).get("title"))
            or thread_id
        )
        cwd = (
            _string((latest_remote or {}).get("cwd"))
            or _string((incoming_snapshot or {}).get("cwd"))
            or _string((local_row or {}).get("cwd"))
        )
        conflicts.append(
            {
                "id": f"device:{thread_id}",
                "source_type": "device",
                "thread_id": thread_id,
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
                "local": _local_thread_payload(local_row, local_fingerprint),
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


def thread_sync_conflicts_payload(
    *,
    normal_home: Path = NORMAL_CODEX_HOME_DIR,
    voice_home: Path = CODEX_HOME_DIR,
    home_ledger_path: Path = DEFAULT_HOME_SYNC_LEDGER_PATH,
    exchange_dir: Path = DEFAULT_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    device_ledger_path: Path = DEFAULT_LEDGER_PATH,
) -> dict[str, Any]:
    """Show unresolved Codex thread sync conflicts across homes and devices."""
    home_conflicts = thread_home_sync_conflicts_payload(
        normal_home=normal_home,
        voice_home=voice_home,
        ledger_path=home_ledger_path,
    )
    device_conflicts = thread_snapshot_conflicts_payload(
        codex_home=voice_home,
        exchange_dir=exchange_dir,
        device_identity_path=device_identity_path,
        ledger_path=device_ledger_path,
    )
    conflicts = [
        *home_conflicts["conflicts"],
        *device_conflicts["conflicts"],
    ]
    conflicts.sort(key=lambda item: item.get("detected_at") or 0, reverse=True)
    return {
        "device": device_conflicts.get("device"),
        "exchange_dir": device_conflicts.get("exchange_dir"),
        "ledger_path": device_conflicts.get("ledger_path"),
        "home_ledger_path": home_conflicts.get("ledger_path"),
        "home_conflict_count": home_conflicts["conflict_count"],
        "device_conflict_count": device_conflicts["conflict_count"],
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
    }


def thread_home_sync_conflicts_payload(
    *,
    normal_home: Path = NORMAL_CODEX_HOME_DIR,
    voice_home: Path = CODEX_HOME_DIR,
    ledger_path: Path = DEFAULT_HOME_SYNC_LEDGER_PATH,
) -> dict[str, Any]:
    ledger = read_scoped_ledger(
        ledger_path,
        scope_key="threads",
        logger=logger,
        malformed_event="codex_thread_sync_ledger_malformed",
    )
    normal_db = normal_home / STATE_DB_NAME
    voice_db = voice_home / STATE_DB_NAME
    conflicts: list[dict[str, Any]] = []
    for thread_id, thread_ledger in ledger.items():
        if not isinstance(thread_id, str) or not isinstance(thread_ledger, dict):
            continue
        if thread_ledger.get("status") != "conflict":
            continue
        normal_row = _exchange_thread_row(normal_db, thread_id)
        voice_row = _exchange_thread_row(voice_db, thread_id)
        normal_fingerprint = _fingerprint_id(
            _thread_fingerprint(normal_row, normal_home, thread_id)
        )
        voice_fingerprint = _fingerprint_id(
            _thread_fingerprint(voice_row, voice_home, thread_id)
        )
        title = (
            _string((voice_row or {}).get("title"))
            or _string((normal_row or {}).get("title"))
            or thread_id
        )
        cwd = _string((voice_row or {}).get("cwd")) or _string(
            (normal_row or {}).get("cwd")
        )
        conflicts.append(
            {
                "id": f"home:{thread_id}",
                "source_type": "home",
                "thread_id": thread_id,
                "title": title,
                "cwd": cwd,
                "reason": _string(thread_ledger.get("reason")) or "conflict",
                "detected_at": thread_ledger.get("synced_at"),
                "normal_fingerprint": normal_fingerprint,
                "voice_fingerprint": voice_fingerprint,
                "local_fingerprint": voice_fingerprint,
                "current_local_fingerprint": voice_fingerprint,
                "normal": _local_thread_payload(normal_row, normal_fingerprint),
                "voice": _local_thread_payload(voice_row, voice_fingerprint),
                "local": _local_thread_payload(voice_row, voice_fingerprint),
                "remote_label": "Normal Codex home",
                "is_resolvable": False,
            }
        )

    return {
        "ledger_path": str(ledger_path),
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
    }


def resolve_thread_snapshot_conflict(
    thread_id: str,
    *,
    action: str,
    codex_home: Path = CODEX_HOME_DIR,
    exchange_dir: Path = DEFAULT_EXCHANGE_DIR,
    device_identity_path: Path = DEFAULT_DEVICE_IDENTITY_PATH,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
    target_user_home: Path | None = None,
) -> dict[str, Any]:
    if action not in {"accept_local", "accept_remote_latest"}:
        raise ThreadConflictResolutionError("unsupported_resolution_action")

    ledger = _read_exchange_ledger(ledger_path)
    thread_ledger = ledger.get("threads", {}).get(thread_id)
    if not isinstance(thread_ledger, dict) or not isinstance(
        thread_ledger.get("conflict"), dict
    ):
        raise ThreadConflictResolutionError("conflict_not_found")

    conflict = thread_ledger["conflict"]
    source_device_id = _string(conflict.get("source_device_id"))
    if not source_device_id:
        raise ThreadConflictResolutionError("source_device_not_found")

    state_db = codex_home / STATE_DB_NAME
    local_row = _exchange_thread_row(state_db, thread_id)
    local_fingerprint = _fingerprint_id(
        _thread_fingerprint(local_row, codex_home, thread_id)
    )
    snapshots = _snapshot_records(
        exchange_dir,
        thread_id=thread_id,
        source_device_id=source_device_id,
    )
    if not snapshots:
        raise ThreadConflictResolutionError("source_snapshots_not_found")

    if action == "accept_remote_latest":
        latest = _latest_snapshot_record(snapshots)
        if latest is None:
            raise ThreadConflictResolutionError("source_snapshots_not_found")
        validation_error = _validate_snapshot(latest["path"], latest["metadata"])
        if validation_error:
            raise ThreadConflictResolutionError(validation_error)
        _import_snapshot_into_home(
            snapshot_dir=latest["path"],
            metadata=latest["metadata"],
            codex_home=codex_home,
            overwrite=local_row is not None,
            target_user_home=target_user_home or Path.home(),
        )
        resolved_fingerprint = _string(latest["metadata"].get("fingerprint"))
        for snapshot in snapshots:
            record_device_snapshot(
                thread_ledger,
                device_id=source_device_id,
                fingerprint_id=snapshot["metadata"]["fingerprint"],
                snapshot_path=snapshot["path"],
                status="imported",
            )
    else:
        if not local_fingerprint:
            raise ThreadConflictResolutionError("local_thread_not_found")
        resolved_fingerprint = local_fingerprint
        for snapshot in snapshots:
            record_device_snapshot(
                thread_ledger,
                device_id=source_device_id,
                fingerprint_id=snapshot["metadata"]["fingerprint"],
                snapshot_path=snapshot["path"],
                status="ignored",
            )

    thread_ledger.pop("conflict", None)
    thread_ledger["local_fingerprint"] = resolved_fingerprint
    thread_ledger["resolved_conflict"] = {
        "action": action,
        "resolved_at": time.time(),
        "source_device_id": source_device_id,
        "fingerprint": resolved_fingerprint,
    }
    _write_exchange_ledger(ledger_path, ledger)
    return {
        "thread_id": thread_id,
        "action": action,
        "fingerprint": resolved_fingerprint,
        "conflicts": thread_snapshot_conflicts_payload(
            codex_home=codex_home,
            exchange_dir=exchange_dir,
            device_identity_path=device_identity_path,
            ledger_path=ledger_path,
        ),
    }


def _write_snapshot(
    *,
    exchange_dir: Path,
    identity: DeviceIdentity,
    codex_home: Path,
    row: dict[str, Any],
    rollout: Path,
    fingerprint: dict[str, Any],
    fingerprint_id: str,
    parent_fingerprint: str | None,
    index_entry: dict[str, Any] | None,
    dynamic_tools: list[dict[str, Any]],
    source_user_home: Path,
) -> Path:
    thread_id = _string(row.get("id"))
    if not thread_id:
        raise ThreadTransferError("thread row missing id")
    target_dir = (
        exchange_dir
        / "devices"
        / identity.device_id
        / "snapshots"
        / thread_id
        / fingerprint_id
    )
    if target_dir.exists():
        return target_dir
    tmp_dir = target_dir.parent / f".tmp-{fingerprint_id}-{uuid.uuid4()}"
    tmp_dir.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copy2(rollout, tmp_dir / "rollout.jsonl")
        metadata = _snapshot_metadata(
            identity=identity,
            codex_home=codex_home,
            row=row,
            rollout=rollout,
            fingerprint=fingerprint,
            fingerprint_id=fingerprint_id,
            parent_fingerprint=parent_fingerprint,
            index_entry=index_entry,
            dynamic_tools=dynamic_tools,
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


def _snapshot_metadata(
    *,
    identity: DeviceIdentity,
    codex_home: Path,
    row: dict[str, Any],
    rollout: Path,
    fingerprint: dict[str, Any],
    fingerprint_id: str,
    parent_fingerprint: str | None,
    index_entry: dict[str, Any] | None,
    dynamic_tools: list[dict[str, Any]],
    source_user_home: Path,
) -> dict[str, Any]:
    thread_row = dict(row)
    thread_row.pop("rollout_path", None)
    try:
        rollout_relative_path = str(rollout.relative_to(codex_home))
    except ValueError:
        rollout_relative_path = str(Path("sessions") / rollout.name)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_device_id": identity.device_id,
        "source_device_name": identity.device_name,
        "source_user_home": str(source_user_home),
        "thread_id": row["id"],
        "fingerprint": fingerprint_id,
        "parent_fingerprint": parent_fingerprint,
        "exported_at": time.time(),
        "rollout_relative_path": rollout_relative_path,
        "rollout_sha256": fingerprint["rollout_sha256"],
        "rollout_size": fingerprint["rollout_size"],
        "thread_row": thread_row,
        "session_index_entry": index_entry,
        "dynamic_tools": dynamic_tools,
    }


def _read_snapshot_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ThreadTransferError("metadata_not_found")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ThreadTransferError("metadata_malformed") from exc
    if not isinstance(metadata, dict):
        raise ThreadTransferError("metadata_malformed")
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ThreadTransferError("unsupported_schema")
    for key in ("source_device_id", "thread_id", "fingerprint", "rollout_sha256"):
        if not _string(metadata.get(key)):
            raise ThreadTransferError(f"metadata_missing_{key}")
    return metadata


def _validate_snapshot(snapshot_dir: Path, metadata: dict[str, Any]) -> str | None:
    rollout = snapshot_dir / "rollout.jsonl"
    fingerprint = _fingerprint_from_rollout_path(rollout, None)
    if fingerprint is None:
        return "rollout_not_found"
    if fingerprint["rollout_sha256"] != metadata.get("rollout_sha256"):
        return "rollout_hash_mismatch"
    if fingerprint["rollout_size"] != metadata.get("rollout_size"):
        return "rollout_size_mismatch"
    return None


def _import_snapshot_into_home(
    *,
    snapshot_dir: Path,
    metadata: dict[str, Any],
    codex_home: Path,
    overwrite: bool,
    target_user_home: Path,
) -> None:
    thread_id = metadata["thread_id"]
    relative_path = Path(_string(metadata.get("rollout_relative_path")) or "sessions")
    if relative_path.is_absolute() or ".." in relative_path.parts:
        relative_path = Path("sessions") / f"rollout-{thread_id}.jsonl"
    target_rollout = codex_home / relative_path
    target_rollout.parent.mkdir(parents=True, exist_ok=True)

    staging_dir = codex_home / IMPORT_STAGING_DIR_NAME
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged_rollout = staging_dir / f"{thread_id}-{uuid.uuid4()}.jsonl"
    backup_rollout: Path | None = None
    rollout_committed = False
    try:
        shutil.copy2(snapshot_dir / "rollout.jsonl", staged_rollout)
        if target_rollout.exists() or target_rollout.is_symlink():
            backup_dir = codex_home / IMPORT_BACKUP_DIR_NAME
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_rollout = backup_dir / f"{thread_id}-{uuid.uuid4()}.jsonl"
            shutil.move(str(target_rollout), str(backup_rollout))
        os.replace(staged_rollout, target_rollout)
        rollout_committed = True
        # State mutations run only after the rollout file commit succeeded,
        # so a failure here rolls the rollout back instead of leaving a
        # half-written import.
        _apply_snapshot_db_state(
            codex_home=codex_home,
            thread_id=thread_id,
            metadata=metadata,
            target_rollout=target_rollout,
            overwrite=overwrite,
            target_user_home=target_user_home,
        )
    except Exception:
        staged_rollout.unlink(missing_ok=True)
        _restore_rollout_backup(
            target_rollout,
            backup_rollout,
            rollout_committed=rollout_committed,
        )
        raise
    finally:
        remove_empty_dir(staging_dir)
    if backup_rollout is not None:
        backup_rollout.unlink(missing_ok=True)
        remove_empty_dir(backup_rollout.parent)


def _restore_rollout_backup(
    target_rollout: Path,
    backup_rollout: Path | None,
    *,
    rollout_committed: bool,
) -> None:
    if backup_rollout is not None and backup_rollout.exists():
        target_rollout.unlink(missing_ok=True)
        shutil.move(str(backup_rollout), str(target_rollout))
        remove_empty_dir(backup_rollout.parent)
        return
    if rollout_committed:
        target_rollout.unlink(missing_ok=True)


def _snapshot_source_user_home(metadata: dict[str, Any]) -> Path | None:
    value = _string(metadata.get("source_user_home"))
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else None


def _translate_existing_thread_cwds(state_db: Path, target_user_home: Path) -> None:
    """Migrate previously imported foreign-home cwd values in place."""
    with closing(_connect(state_db)) as conn:
        conn.row_factory = sqlite3.Row
        if not _has_table(conn, "threads") or "cwd" not in _table_columns(
            conn, "threads"
        ):
            return
        rows = conn.execute("SELECT id, cwd FROM threads").fetchall()
        for row in rows:
            translated = translate_home_path(row["cwd"], target_home=target_user_home)
            if translated and translated != row["cwd"]:
                conn.execute(
                    "UPDATE threads SET cwd = ? WHERE id = ?",
                    (translated, row["id"]),
                )
        conn.commit()


def _apply_snapshot_db_state(
    *,
    codex_home: Path,
    thread_id: str,
    metadata: dict[str, Any],
    target_rollout: Path,
    overwrite: bool,
    target_user_home: Path,
) -> None:
    thread_row = metadata.get("thread_row")
    thread_row = thread_row if isinstance(thread_row, dict) else {}
    thread_row = dict(thread_row)
    source_user_home = _snapshot_source_user_home(metadata)
    translated_cwd = translate_home_path(
        _string(thread_row.get("cwd")),
        source_home=source_user_home,
        target_home=target_user_home,
    )
    if translated_cwd is not None:
        thread_row["cwd"] = translated_cwd
    dynamic_tools = metadata.get("dynamic_tools")
    dynamic_tools = dynamic_tools if isinstance(dynamic_tools, list) else []
    with closing(_connect(codex_home / STATE_DB_NAME)) as conn:
        _upsert_thread_row(
            conn, thread_id, thread_row, target_rollout, overwrite=overwrite
        )
        _replace_thread_dynamic_tools(conn, thread_id, dynamic_tools)
        conn.commit()
    _append_session_index_metadata(
        codex_home / SESSION_INDEX_NAME,
        metadata.get("session_index_entry"),
        thread_id=thread_id,
        fallback_title=_string(thread_row.get("title")) or thread_id,
    )


def _upsert_thread_row(
    conn: sqlite3.Connection,
    thread_id: str,
    row: dict[str, Any],
    rollout_path: Path,
    *,
    overwrite: bool,
) -> None:
    target_columns = _table_columns(conn, "threads")
    values = {key: value for key, value in row.items() if key in target_columns}
    values["id"] = thread_id
    values["rollout_path"] = str(rollout_path)
    columns = [column for column in target_columns if column in values]
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    verb = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
    conn.execute(
        f"{verb} INTO threads ({column_sql}) VALUES ({placeholders})",
        [values[column] for column in columns],
    )


def _replace_thread_dynamic_tools(
    conn: sqlite3.Connection,
    thread_id: str,
    rows: list[Any],
) -> None:
    if not _has_table(conn, "thread_dynamic_tools"):
        return
    columns = _table_columns(conn, "thread_dynamic_tools")
    conn.execute("DELETE FROM thread_dynamic_tools WHERE thread_id = ?", (thread_id,))
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        values = {key: value for key, value in raw_row.items() if key in columns}
        values["thread_id"] = thread_id
        insert_columns = [column for column in columns if column in values]
        placeholders = ", ".join("?" for _ in insert_columns)
        column_sql = ", ".join(insert_columns)
        conn.execute(
            f"INSERT OR REPLACE INTO thread_dynamic_tools ({column_sql}) VALUES ({placeholders})",
            [values[column] for column in insert_columns],
        )


def _append_session_index_metadata(
    path: Path,
    entry: Any,
    *,
    thread_id: str,
    fallback_title: str,
) -> None:
    index_entry = dict(entry) if isinstance(entry, dict) else {}
    index_entry["id"] = thread_id
    index_entry.setdefault("thread_name", fallback_title)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(index_entry, separators=(",", ":")) + "\n")


def _thread_dynamic_tools(db_path: Path, thread_id: str) -> list[dict[str, Any]]:
    with closing(_connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        if not _has_table(conn, "thread_dynamic_tools"):
            return []
        columns = _table_columns(conn, "thread_dynamic_tools")
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM thread_dynamic_tools WHERE thread_id = ? ORDER BY position",
            (thread_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _exchange_thread_row(db_path: Path, thread_id: str) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    with closing(_connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def _fingerprint_id(fingerprint: dict[str, Any] | None) -> str | None:
    if not fingerprint:
        return None
    return _string(fingerprint.get("rollout_sha256"))


def _read_exchange_ledger(path: Path) -> dict[str, Any]:
    return read_device_ledger(
        path,
        scope_key="threads",
        logger=logger,
        malformed_event="codex_thread_exchange event=ledger_malformed",
    )


def _write_exchange_ledger(path: Path, ledger: dict[str, Any]) -> None:
    write_json_atomic(path, ledger)


def _snapshot_records(
    exchange_dir: Path,
    *,
    thread_id: str,
    source_device_id: str | None = None,
) -> list[dict[str, Any]]:
    return collect_snapshot_records(
        exchange_dir,
        entity_id=thread_id,
        entity_id_key="thread_id",
        read_metadata=_read_snapshot_metadata,
        metadata_error=ThreadTransferError,
        source_device_id=source_device_id,
    )


_find_snapshot_record = find_snapshot_record


def _latest_snapshot_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    return max(records, key=_snapshot_record_sort_key)


def _snapshot_record_sort_key(record: dict[str, Any]) -> tuple[float, float, str]:
    metadata = record["metadata"]
    row = metadata.get("thread_row")
    row = row if isinstance(row, dict) else {}
    updated_at_ms = row.get("updated_at_ms")
    if isinstance(updated_at_ms, int | float):
        updated_value = float(updated_at_ms)
    else:
        updated_at = row.get("updated_at")
        updated_value = (
            float(updated_at) * 1000 if isinstance(updated_at, int | float) else 0
        )
    exported_at = metadata.get("exported_at")
    exported_value = float(exported_at) if isinstance(exported_at, int | float) else 0
    return (
        updated_value,
        exported_value,
        _string(metadata.get("fingerprint")) or "",
    )


def _snapshot_payload(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    metadata = record["metadata"]
    row = metadata.get("thread_row")
    row = row if isinstance(row, dict) else {}
    return {
        "fingerprint": metadata.get("fingerprint"),
        "parent_fingerprint": metadata.get("parent_fingerprint"),
        "source_device_id": metadata.get("source_device_id"),
        "source_device_name": metadata.get("source_device_name"),
        "snapshot_path": str(record["path"]),
        "rollout_size": metadata.get("rollout_size"),
        "exported_at": metadata.get("exported_at"),
        "updated_at_ms": row.get("updated_at_ms"),
        "title": _string(row.get("title")) or metadata.get("thread_id"),
        "cwd": _string(row.get("cwd")),
        "tokens_used": row.get("tokens_used"),
    }


def _local_thread_payload(
    row: dict[str, Any] | None,
    fingerprint: str | None,
) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "fingerprint": fingerprint,
        "updated_at_ms": row.get("updated_at_ms"),
        "title": _string(row.get("title")) or _string(row.get("id")),
        "cwd": _string(row.get("cwd")),
        "tokens_used": row.get("tokens_used"),
        "rollout_path": _string(row.get("rollout_path")),
    }
