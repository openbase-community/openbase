"""Claude session filesystem: fingerprints, snapshot reads, copy/staging, device I/O."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from .claude_jsonl import _decode_project_key, _mtime_ms, _parse_claude_jsonl
from .claude_models import (
    IMPORT_BACKUP_DIR_NAME,
    IMPORT_STAGING_DIR_NAME,
    SCHEMA_VERSION,
    ClaudeSessionSnapshot,
)
from .thread_import import _rollout_open_for_write, _string
from .thread_sync_common import (
    DeviceIdentity,
    collect_snapshot_records,
    path_stable,
    remove_empty_dir,
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


def _same_fingerprint(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left.get("tree_sha256") == right.get("tree_sha256")


def _optional_fingerprint_id(snapshot: ClaudeSessionSnapshot | None) -> str | None:
    if snapshot is None:
        return None
    return _string(snapshot.fingerprint.get("tree_sha256"))


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
