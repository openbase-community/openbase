"""Claude session sync conflict payload builders and resolution."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from openbase_coder_cli.paths import (
    NORMAL_CLAUDE_CONFIG_DIR,
    OPENBASE_CLAUDE_CONFIG_DIR,
)

from .claude_ledger import (
    _read_device_ledger,
    _read_sync_ledger,
    _write_device_ledger,
)
from .claude_models import (
    DEFAULT_DEVICE_EXCHANGE_DIR,
    DEFAULT_DEVICE_LEDGER_PATH,
    DEFAULT_SYNC_LEDGER_PATH,
    ClaudeConflictResolutionError,
)
from .claude_session_db import (
    _active_claude_session_ids,
    _backfill_openbase_session_metadata,
    _translated_metadata_cwd,
)
from .claude_snapshot_io import (
    _find_local_session_root,
    _import_device_snapshot_into_home,
    _latest_snapshot_record,
    _local_session_payload,
    _optional_fingerprint_id,
    _read_session_snapshot,
    _snapshot_payload,
    _snapshot_records,
    _validate_device_snapshot,
)
from .thread_exchange import DEFAULT_DEVICE_IDENTITY_PATH
from .thread_import import _string
from .thread_sync_common import (
    find_snapshot_record,
    merged_sync_conflicts_payload,
    read_device_identity,
    record_device_snapshot,
)


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
