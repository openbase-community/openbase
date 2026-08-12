"""Shared helpers for thread sync implementations."""

from __future__ import annotations

import json
import logging
import os
import platform
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The super-agents package keeps its single SQLite agent store in the
# Claude Code app dir (see ``super_agents.agent_store.app_dir``); both
# backends' Super Agents sessions are recorded there.
DEFAULT_SUPER_AGENTS_STORE_HOME = (
    Path.home() / ".local" / "share" / "super-agents-claude-code"
)
SUPER_AGENTS_STORE_HOME_ENV = "SUPER_AGENTS_CLAUDE_CODE_HOME"


def translate_home_path(
    value: str | None,
    *,
    target_home: Path | None = None,
    source_home: Path | None = None,
) -> str | None:
    """Translate a source device's home-relative path onto this device.

    Older snapshots did not record ``source_home``, so recognize conventional
    macOS and Linux home roots as a backward-compatible fallback. Paths outside
    a recognized/source home are deliberately preserved even when absent.
    """
    if not value:
        return value
    target = target_home or Path.home()
    path = Path(value).expanduser()
    if not path.is_absolute():
        return value
    source = source_home or _recognized_user_home(path)
    if source is None:
        return value
    try:
        relative = path.relative_to(source)
    except ValueError:
        return value
    return str(target / relative)


def _recognized_user_home(path: Path) -> Path | None:
    parts = path.parts
    if len(parts) >= 3 and parts[1] in {"Users", "home"}:
        return Path(*parts[:3])
    if len(parts) >= 2 and parts[1] == "root":
        return Path("/root")
    return None


def super_agents_state_db_path() -> Path:
    configured = os.environ.get(SUPER_AGENTS_STORE_HOME_ENV)
    home = (
        Path(configured).expanduser() if configured else DEFAULT_SUPER_AGENTS_STORE_HOME
    )
    return home / "state.sqlite3"


def remove_empty_dir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        return


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    device_name: str
    created_at: float

    def to_json(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "created_at": self.created_at,
        }


def get_or_create_device_identity(path: Path) -> DeviceIdentity:
    existing = read_device_identity(path)
    if existing is not None:
        return existing
    identity = DeviceIdentity(
        device_id=str(uuid.uuid4()),
        device_name=platform.node() or "unknown-device",
        created_at=time.time(),
    )
    write_json_atomic(path, identity.to_json())
    return identity


def read_device_identity(path: Path) -> DeviceIdentity | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    device_id = raw.get("device_id")
    device_name = raw.get("device_name")
    created_at = raw.get("created_at")
    if not isinstance(device_id, str) or not isinstance(device_name, str):
        return None
    if not isinstance(created_at, int | float):
        created_at = 0.0
    return DeviceIdentity(device_id, device_name, float(created_at))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as tmp:
        tmp.write(body)
        tmp_name = tmp.name
    os.replace(tmp_name, path)


def read_scoped_ledger(
    path: Path,
    *,
    scope_key: str,
    logger: logging.Logger,
    malformed_event: str,
) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("%s path=%s", malformed_event, path)
        return {}
    if not isinstance(raw, dict):
        return {}
    entries = raw.get(scope_key)
    return entries if isinstance(entries, dict) else {}


def write_scoped_ledger(path: Path, *, scope_key: str, ledger: dict[str, Any]) -> None:
    write_json_atomic(path, {scope_key: ledger})


def read_device_ledger(
    path: Path,
    *,
    scope_key: str,
    logger: logging.Logger,
    malformed_event: str,
) -> dict[str, Any]:
    if not path.exists():
        return {scope_key: {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("%s path=%s", malformed_event, path)
        return {scope_key: {}}
    if not isinstance(raw, dict):
        return {scope_key: {}}
    if not isinstance(raw.get(scope_key), dict):
        raw[scope_key] = {}
    return raw


def device_ledger_entry(
    ledger: dict[str, Any],
    *,
    scope_key: str,
    entity_id: str,
) -> dict[str, Any]:
    entries = ledger.setdefault(scope_key, {})
    entry = entries.setdefault(entity_id, {})
    entry.setdefault("devices", {})
    return entry


def device_exported_fingerprints(
    entry_ledger: dict[str, Any], device_id: str
) -> set[str]:
    device = entry_ledger.get("devices", {}).get(device_id)
    snapshots = device.get("snapshots") if isinstance(device, dict) else None
    if not isinstance(snapshots, dict):
        return set()
    return {
        fingerprint
        for fingerprint, value in snapshots.items()
        if isinstance(fingerprint, str)
        and isinstance(value, dict)
        and value.get("status") == "exported"
    }


def snapshot_already_imported(
    entry_ledger: dict[str, Any],
    *,
    device_id: str,
    fingerprint_id: str,
) -> bool:
    device = entry_ledger.get("devices", {}).get(device_id)
    snapshots = device.get("snapshots") if isinstance(device, dict) else None
    snapshot = snapshots.get(fingerprint_id) if isinstance(snapshots, dict) else None
    return isinstance(snapshot, dict) and snapshot.get("status") in {
        "ignored",
        "imported",
        "same_content",
    }


def record_device_snapshot(
    entry_ledger: dict[str, Any],
    *,
    device_id: str,
    fingerprint_id: str,
    snapshot_path: Path,
    status: str,
) -> None:
    devices = entry_ledger.setdefault("devices", {})
    device = devices.setdefault(device_id, {})
    snapshots = device.setdefault("snapshots", {})
    snapshots[fingerprint_id] = {
        "fingerprint": fingerprint_id,
        "snapshot_path": str(snapshot_path),
        "status": status,
        "seen_at": time.time(),
    }


def record_device_conflict(
    entry_ledger: dict[str, Any],
    *,
    local_fingerprint: str | None,
    incoming_fingerprint: str,
    source_device_id: str,
    reason: str,
    snapshot_path: Path | None = None,
) -> None:
    conflict = {
        "reason": reason,
        "local_fingerprint": local_fingerprint,
        "incoming_fingerprint": incoming_fingerprint,
        "source_device_id": source_device_id,
        "detected_at": time.time(),
    }
    if snapshot_path is not None:
        conflict["snapshot_path"] = str(snapshot_path)
    entry_ledger["conflict"] = conflict


def file_content_relation(local_path: Path, snapshot_path: Path) -> str | None:
    """Byte-compare an append-only local artifact with a snapshot copy.

    Returns ``identical`` when the contents match, ``local_extends`` when the
    snapshot is a strict byte-prefix of the local file, ``snapshot_extends``
    when the local file is a strict byte-prefix of the snapshot, or ``None``
    when the contents genuinely diverge or either file is unreadable.
    """
    try:
        local_size = local_path.stat().st_size
        snapshot_size = snapshot_path.stat().st_size
        shared = min(local_size, snapshot_size)
        with local_path.open("rb") as local_file, snapshot_path.open("rb") as snap_file:
            remaining = shared
            while remaining:
                chunk = min(remaining, 1024 * 1024)
                if local_file.read(chunk) != snap_file.read(chunk):
                    return None
                remaining -= chunk
    except OSError:
        return None
    if local_size == snapshot_size:
        return "identical"
    return "local_extends" if local_size > snapshot_size else "snapshot_extends"


def import_snapshot_decision(
    *,
    has_local: bool,
    local_fingerprint: str | None,
    incoming_fingerprint: str,
    parent_fingerprint: str | None,
    entry_ledger: dict[str, Any],
) -> str:
    if local_fingerprint == incoming_fingerprint:
        return "same_content"
    if not has_local:
        return "import"
    if parent_fingerprint and local_fingerprint == parent_fingerprint:
        return "import"
    if (
        entry_ledger.get("local_fingerprint") == parent_fingerprint
        and parent_fingerprint
    ):
        return "import"
    return "conflict"


def parent_fingerprint_for_export(
    entry_ledger: dict[str, Any],
    fingerprint_id: str,
) -> str | None:
    parent = entry_ledger.get("local_fingerprint")
    if not isinstance(parent, str):
        return None
    return parent if parent and parent != fingerprint_id else None


def record_synced_pair(
    ledger: dict[str, Any],
    *,
    entity_key: str,
    entity_id: str,
    left_key: str,
    left_fingerprint: dict[str, Any],
    right_key: str,
    right_fingerprint: dict[str, Any],
    reason: str,
) -> None:
    ledger[entity_id] = {
        entity_key: entity_id,
        left_key: left_fingerprint,
        right_key: right_fingerprint,
        "status": "synced",
        "reason": reason,
        "synced_at": time.time(),
    }


def record_sync_conflict(
    ledger: dict[str, Any],
    *,
    entity_key: str,
    entity_id: str,
    left_key: str,
    left_fingerprint: dict[str, Any],
    right_key: str,
    right_fingerprint: dict[str, Any],
    reason: str,
) -> None:
    ledger[entity_id] = {
        entity_key: entity_id,
        left_key: left_fingerprint,
        right_key: right_fingerprint,
        "status": "conflict",
        "reason": reason,
        "synced_at": time.time(),
    }


def fingerprint_matches(
    value: Any,
    fingerprint: dict[str, Any],
    *,
    keys: tuple[str, ...],
) -> bool:
    return isinstance(value, dict) and all(
        value.get(key) == fingerprint.get(key) for key in keys
    )


def ledger_sync_decision(
    previous: Any,
    *,
    left_key: str,
    right_key: str,
    left_fingerprint: dict[str, Any],
    right_fingerprint: dict[str, Any],
    fingerprint_keys: tuple[str, ...],
) -> str:
    """Classify a home pair against its previous ledger entry.

    Returns one of ``both_changed``, ``conflict_unresolved``, ``left_changed``,
    ``right_changed``, or ``ledger_current``.
    """
    if not isinstance(previous, dict):
        return "both_changed"
    if previous.get("status") == "conflict":
        return "conflict_unresolved"
    left_changed = not fingerprint_matches(
        previous.get(left_key), left_fingerprint, keys=fingerprint_keys
    )
    right_changed = not fingerprint_matches(
        previous.get(right_key), right_fingerprint, keys=fingerprint_keys
    )
    if left_changed and right_changed:
        return "both_changed"
    if left_changed:
        return "left_changed"
    if right_changed:
        return "right_changed"
    return "ledger_current"


def sync_cutoff_ms(max_age_days: int | None) -> int | None:
    if max_age_days is None:
        return None
    return int((time.time() - max(max_age_days, 0) * 24 * 60 * 60) * 1000)


def path_stable(path: Path, delay_seconds: float) -> bool:
    before = path.stat()
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    after = path.stat()
    return before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns


def device_snapshot_dirs(exchange_dir: Path) -> list[Path]:
    root = exchange_dir / "devices"
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.glob("*/snapshots/*/*")
        if path.is_dir() and (path / "metadata.json").exists()
    )


def collect_snapshot_records(
    exchange_dir: Path,
    *,
    entity_id: str,
    entity_id_key: str,
    read_metadata: Callable[[Path], dict[str, Any]],
    metadata_error: type[Exception],
    source_device_id: str | None = None,
) -> list[dict[str, Any]]:
    """List exchange snapshots for one entity as {path, metadata} records."""
    records: list[dict[str, Any]] = []
    for snapshot_dir in device_snapshot_dirs(exchange_dir):
        try:
            metadata = read_metadata(snapshot_dir / "metadata.json")
        except metadata_error:
            continue
        if metadata[entity_id_key] != entity_id:
            continue
        if source_device_id and metadata["source_device_id"] != source_device_id:
            continue
        records.append({"path": snapshot_dir, "metadata": metadata})
    return records


def find_snapshot_record(
    records: list[dict[str, Any]],
    fingerprint_id: str | None,
) -> dict[str, Any] | None:
    if not fingerprint_id:
        return None
    return next(
        (
            record
            for record in records
            if record["metadata"].get("fingerprint") == fingerprint_id
        ),
        None,
    )


@dataclass(frozen=True)
class SnapshotExportCandidate:
    """One exportable entity, or a skip decision made by the session source."""

    entity_id: str
    skip_reason: str | None = None
    fingerprint_id: str | None = None
    write_snapshot: Callable[[str | None], Path] | None = None


@dataclass(frozen=True)
class LocalSnapshotState:
    """Local counterpart of an incoming snapshot, as seen by a session source."""

    exists: bool
    fingerprint_id: str | None
    context: Any = None


@dataclass(frozen=True)
class SnapshotImportSource:
    """Session-source callbacks that parameterize the shared import loop."""

    scope_key: str
    entity_id_key: str
    read_metadata: Callable[[Path], dict[str, Any]]
    metadata_error: type[Exception]
    validate_snapshot: Callable[[Path, dict[str, Any]], str | None]
    load_local: Callable[[dict[str, Any]], LocalSnapshotState]
    import_blocked_reason: Callable[[dict[str, Any], LocalSnapshotState], str | None]
    perform_import: Callable[[Path, dict[str, Any], LocalSnapshotState], str | None]
    conflict_includes_snapshot_path: bool = False
    # Optional byte-level comparison between the local artifact and the
    # snapshot (see ``file_content_relation`` return values). Fingerprints
    # include volatile metadata, so two devices can report divergence while
    # holding equal or strictly-ordered content; this callback lets the
    # import loop fast-forward or ignore such snapshots instead of minting a
    # conflict, and auto-clear an existing conflict once content converges.
    compare_content: (
        Callable[[Path, dict[str, Any], LocalSnapshotState], str | None] | None
    ) = None


def run_snapshot_export(
    *,
    candidates: Iterable[SnapshotExportCandidate],
    device_id: str,
    ledger: dict[str, Any],
    scope_key: str,
    result_factory: Callable[..., Any],
) -> list[Any]:
    return [
        _export_one_candidate(
            candidate,
            device_id=device_id,
            ledger=ledger,
            scope_key=scope_key,
            result_factory=result_factory,
        )
        for candidate in candidates
    ]


def _export_one_candidate(
    candidate: SnapshotExportCandidate,
    *,
    device_id: str,
    ledger: dict[str, Any],
    scope_key: str,
    result_factory: Callable[..., Any],
) -> Any:
    if candidate.skip_reason is not None:
        return result_factory(candidate.entity_id, "skipped", candidate.skip_reason)
    fingerprint_id = candidate.fingerprint_id
    entry_ledger = device_ledger_entry(
        ledger, scope_key=scope_key, entity_id=candidate.entity_id
    )
    if fingerprint_id in device_exported_fingerprints(entry_ledger, device_id):
        entry_ledger["local_fingerprint"] = fingerprint_id
        return result_factory(
            candidate.entity_id,
            "already_exported",
            "fingerprint_current",
            None,
            fingerprint_id,
        )
    parent_fingerprint = parent_fingerprint_for_export(entry_ledger, fingerprint_id)
    snapshot_path = candidate.write_snapshot(parent_fingerprint)
    record_device_snapshot(
        entry_ledger,
        device_id=device_id,
        fingerprint_id=fingerprint_id,
        snapshot_path=snapshot_path,
        status="exported",
    )
    entry_ledger["local_fingerprint"] = fingerprint_id
    return result_factory(
        candidate.entity_id,
        "exported",
        "snapshot_written",
        str(snapshot_path),
        fingerprint_id,
        device_id,
    )


def run_snapshot_import(
    *,
    exchange_dir: Path,
    device_id: str,
    ledger: dict[str, Any],
    source: SnapshotImportSource,
    result_factory: Callable[..., Any],
) -> list[Any]:
    # Oldest-first so that within one pass a stale divergent snapshot is
    # processed before the newer one that proves convergence and clears the
    # conflict, never the other way around.
    return [
        _import_one_snapshot(
            snapshot_dir,
            device_id=device_id,
            ledger=ledger,
            source=source,
            result_factory=result_factory,
        )
        for snapshot_dir in sorted(
            device_snapshot_dirs(exchange_dir), key=_snapshot_exported_at
        )
    ]


def _snapshot_exported_at(snapshot_dir: Path) -> float:
    try:
        raw = json.loads((snapshot_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    value = raw.get("exported_at") if isinstance(raw, dict) else None
    return float(value) if isinstance(value, int | float) else 0.0


def _import_one_snapshot(
    snapshot_dir: Path,
    *,
    device_id: str,
    ledger: dict[str, Any],
    source: SnapshotImportSource,
    result_factory: Callable[..., Any],
) -> Any:
    try:
        metadata = source.read_metadata(snapshot_dir / "metadata.json")
    except source.metadata_error as exc:
        return result_factory(
            snapshot_dir.parent.name, "skipped", str(exc), str(snapshot_dir)
        )

    entity_id = metadata[source.entity_id_key]
    source_device_id = metadata["source_device_id"]
    fingerprint_id = metadata["fingerprint"]

    def result(status: str, reason: str) -> Any:
        return result_factory(
            entity_id,
            status,
            reason,
            str(snapshot_dir),
            fingerprint_id,
            source_device_id,
        )

    if source_device_id == device_id:
        return result("skipped", "same_device")

    entry_ledger = device_ledger_entry(
        ledger, scope_key=source.scope_key, entity_id=entity_id
    )
    if snapshot_already_imported(
        entry_ledger, device_id=source_device_id, fingerprint_id=fingerprint_id
    ):
        return result("already_imported", "fingerprint_seen")
    had_conflict = isinstance(entry_ledger.get("conflict"), dict)

    def unresolved_conflict() -> Any:
        record_device_snapshot(
            entry_ledger,
            device_id=source_device_id,
            fingerprint_id=fingerprint_id,
            snapshot_path=snapshot_dir,
            status="seen_after_conflict",
        )
        return result("conflict", "conflict_unresolved")

    if had_conflict and source.compare_content is None:
        return unresolved_conflict()

    validation_error = source.validate_snapshot(snapshot_dir, metadata)
    if validation_error:
        if had_conflict:
            return unresolved_conflict()
        return result("skipped", validation_error)

    local = source.load_local(metadata)
    relation: str | None = None
    if had_conflict and source.compare_content is not None:
        relation = source.compare_content(snapshot_dir, metadata, local)
        if relation is None:
            return unresolved_conflict()
        # The sides converged (or became strictly ordered) after the
        # conflict was minted, so it no longer describes real divergence.
        # Snapshots seen while the conflict stood are retired alongside it,
        # so a stale divergent one cannot re-mint the conflict just cleared.
        entry_ledger.pop("conflict", None)
        for device in entry_ledger.get("devices", {}).values():
            snapshots = device.get("snapshots") if isinstance(device, dict) else None
            if not isinstance(snapshots, dict):
                continue
            for snapshot in snapshots.values():
                if (
                    isinstance(snapshot, dict)
                    and snapshot.get("status") == "seen_after_conflict"
                    and snapshot.get("fingerprint") != fingerprint_id
                ):
                    snapshot["status"] = "ignored"
        logger.info(
            "thread_device_sync event=conflict_auto_cleared entity_id=%s "
            "relation=%s source_device_id=%s",
            entity_id,
            relation,
            source_device_id,
        )

    parent_fingerprint = metadata.get("parent_fingerprint")
    if not isinstance(parent_fingerprint, str) or not parent_fingerprint:
        parent_fingerprint = None
    decision = import_snapshot_decision(
        has_local=local.exists,
        local_fingerprint=local.fingerprint_id,
        incoming_fingerprint=fingerprint_id,
        parent_fingerprint=parent_fingerprint,
        entry_ledger=entry_ledger,
    )
    if decision == "same_content":
        record_device_snapshot(
            entry_ledger,
            device_id=source_device_id,
            fingerprint_id=fingerprint_id,
            snapshot_path=snapshot_dir,
            status="same_content",
        )
        return result("already_imported", "same_content")
    if decision == "conflict":
        if relation is None and source.compare_content is not None:
            relation = source.compare_content(snapshot_dir, metadata, local)
        if relation == "identical":
            record_device_snapshot(
                entry_ledger,
                device_id=source_device_id,
                fingerprint_id=fingerprint_id,
                snapshot_path=snapshot_dir,
                status="same_content",
            )
            return result("already_imported", "same_content_bytes")
        if relation == "local_extends":
            record_device_snapshot(
                entry_ledger,
                device_id=source_device_id,
                fingerprint_id=fingerprint_id,
                snapshot_path=snapshot_dir,
                status="ignored",
            )
            return result("skipped", "superseded_by_local")
        if relation != "snapshot_extends":
            record_device_snapshot(
                entry_ledger,
                device_id=source_device_id,
                fingerprint_id=fingerprint_id,
                snapshot_path=snapshot_dir,
                status="seen_after_conflict",
            )
            record_device_conflict(
                entry_ledger,
                local_fingerprint=local.fingerprint_id,
                incoming_fingerprint=fingerprint_id,
                source_device_id=source_device_id,
                reason="divergent_fingerprint",
                snapshot_path=snapshot_dir
                if source.conflict_includes_snapshot_path
                else None,
            )
            return result("conflict", "divergent_fingerprint")
        # The snapshot strictly extends the local artifact: a safe
        # append-only fast-forward despite the fingerprint mismatch.

    blocked_reason = source.import_blocked_reason(metadata, local)
    if blocked_reason:
        return result("skipped", blocked_reason)

    error_reason = source.perform_import(snapshot_dir, metadata, local)
    if error_reason:
        return result("error", error_reason)

    record_device_snapshot(
        entry_ledger,
        device_id=source_device_id,
        fingerprint_id=fingerprint_id,
        snapshot_path=snapshot_dir,
        status="imported",
    )
    entry_ledger["local_fingerprint"] = fingerprint_id
    return result("imported", "snapshot_imported")
