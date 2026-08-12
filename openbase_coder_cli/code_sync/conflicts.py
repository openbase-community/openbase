"""Conflict records for code-sync (repo divergence and file conflicts).

Records live in ``~/.openbase/code-sync/conflicts.json`` and are surfaced in
the console/iOS alongside thread sync conflicts. Branch records clear when
the repository manifest brings both heads back together; explicit resolution
remains available when safe automatic convergence is blocked.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from openbase_coder_cli.code_sync import CodeSyncError
from openbase_coder_cli.paths import CODE_SYNC_CONFLICTS_PATH
from openbase_coder_cli.sync_config import folder_for_id

BRANCH_CONFLICT_KIND = "branch"
FILE_CONFLICT_KIND = "file"
RESOLVE_ACTIONS = ("keep_local", "use_remote")
STASH_BACKUP_MESSAGE = "code-sync-backup"


def read_conflicts(path: Path | None = None) -> list[dict[str, Any]]:
    conflicts_path = path or CODE_SYNC_CONFLICTS_PATH
    try:
        payload = json.loads(conflicts_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    conflicts = payload.get("conflicts") if isinstance(payload, dict) else None
    return conflicts if isinstance(conflicts, list) else []


def unresolved_conflicts(path: Path | None = None) -> list[dict[str, Any]]:
    return [
        conflict for conflict in read_conflicts(path) if not conflict.get("resolved")
    ]


def record_branch_conflict(
    *,
    folder_id: str,
    repo_relpath: str,
    branch: str,
    local_sha: str,
    remote_sha: str,
    path: Path | None = None,
) -> dict[str, Any]:
    """Record a repo divergence, deduped per (folder, repo, branch)."""
    conflicts = read_conflicts(path)
    for conflict in conflicts:
        if (
            not conflict.get("resolved")
            and conflict.get("kind") == BRANCH_CONFLICT_KIND
            and conflict.get("folder_id") == folder_id
            and conflict.get("repo_relpath") == repo_relpath
            and conflict.get("branch") == branch
        ):
            conflict.update(
                {
                    "local_sha": local_sha,
                    "remote_sha": remote_sha,
                    "detected_at": _timestamp(),
                }
            )
            _write_conflicts(conflicts, path)
            return conflict

    record = {
        "id": uuid.uuid4().hex,
        "kind": BRANCH_CONFLICT_KIND,
        "folder_id": folder_id,
        "repo_relpath": repo_relpath,
        "branch": branch,
        "local_sha": local_sha,
        "remote_sha": remote_sha,
        "detected_at": _timestamp(),
        "resolved": False,
    }
    conflicts.append(record)
    _write_conflicts(conflicts, path)
    return record


def mark_branch_conflicts_resolved(
    *,
    folder_id: str,
    repo_relpath: str,
    branch: str,
    resolution: str = "converged",
    path: Path | None = None,
) -> int:
    """Resolve stale branch-conflict records after both refs converge."""
    conflicts = read_conflicts(path)
    resolved_count = 0
    now = _timestamp()
    for conflict in conflicts:
        if (
            conflict.get("resolved")
            or conflict.get("kind") != BRANCH_CONFLICT_KIND
            or conflict.get("folder_id") != folder_id
            or conflict.get("repo_relpath") != repo_relpath
            or conflict.get("branch") != branch
        ):
            continue
        conflict.update(
            {
                "resolved": True,
                "resolution": resolution,
                "resolved_at": now,
            }
        )
        resolved_count += 1
    if resolved_count:
        _write_conflicts(conflicts, path)
    return resolved_count


def record_file_conflict(
    *, folder_id: str, file_relpath: str, path: Path | None = None
) -> dict[str, Any]:
    """Record a Syncthing ``*.sync-conflict-*`` copy for cleanup."""
    conflicts = read_conflicts(path)
    for conflict in conflicts:
        if (
            not conflict.get("resolved")
            and conflict.get("kind") == FILE_CONFLICT_KIND
            and conflict.get("folder_id") == folder_id
            and conflict.get("path") == file_relpath
        ):
            return conflict
    record = {
        "id": uuid.uuid4().hex,
        "kind": FILE_CONFLICT_KIND,
        "folder_id": folder_id,
        "path": file_relpath,
        "detected_at": _timestamp(),
        "resolved": False,
    }
    conflicts.append(record)
    _write_conflicts(conflicts, path)
    return record


def find_conflict(conflict_id: str, path: Path | None = None) -> dict[str, Any] | None:
    for conflict in read_conflicts(path):
        if conflict.get("id") == conflict_id:
            return conflict
    return None


def containing_folder_for_conflict_path(file_relpath: str) -> str | None:
    """Containing folder for a home-relative Syncthing conflict copy."""
    conflict_path = PurePosixPath(file_relpath)
    if (
        not file_relpath
        or conflict_path.is_absolute()
        or any(part == ".." for part in conflict_path.parts)
    ):
        raise CodeSyncError("Conflict record has an invalid file path.")
    containing_folder = conflict_path.parent
    if str(containing_folder) in ("", "."):
        return None
    return containing_folder.as_posix()


def original_relpath_for_conflict_path(file_relpath: str) -> str:
    """Original file relpath for a Syncthing conflict-copy relpath."""
    conflict_path = PurePosixPath(file_relpath)
    original_name = _original_name(conflict_path.name)
    if str(conflict_path.parent) in ("", "."):
        return original_name
    return str(conflict_path.with_name(original_name))


def conflict_device_hint(file_relpath: str) -> str:
    """Best-effort device token parsed from Syncthing's conflict filename."""
    conflict_name = PurePosixPath(file_relpath).name
    _stem, marker, remainder = conflict_name.partition(".sync-conflict-")
    if not marker:
        return ""
    marker_payload = remainder.split(".", 1)[0]
    parts = marker_payload.split("-")
    return "-".join(parts[2:]) if len(parts) >= 3 else ""


def ignore_pattern_for_containing_folder(conflict: dict[str, Any]) -> str:
    """Return the anchored Syncthing ignore pattern for a file conflict folder."""
    if conflict.get("kind") != FILE_CONFLICT_KIND:
        raise CodeSyncError("Only file conflicts have a containing folder to ignore.")
    file_relpath = str(conflict.get("path") or "")
    containing_folder = containing_folder_for_conflict_path(file_relpath)
    if containing_folder is None:
        raise CodeSyncError(
            "This conflict is at the sync folder root, so there is no containing "
            "folder to ignore."
        )
    return f"/{containing_folder}"


def mark_file_conflicts_resolved_under(
    *, folder_id: str, folder_relpath: str, resolution: str, path: Path | None = None
) -> int:
    """Mark unresolved file conflicts under a folder path as resolved."""
    normalized = folder_relpath.strip("/")
    conflicts = read_conflicts(path)
    resolved_count = 0
    now = _timestamp()
    for conflict in conflicts:
        if (
            conflict.get("resolved")
            or conflict.get("kind") != FILE_CONFLICT_KIND
            or conflict.get("folder_id") != folder_id
        ):
            continue
        file_relpath = str(conflict.get("path") or "")
        if file_relpath == normalized or file_relpath.startswith(f"{normalized}/"):
            conflict.update(
                {
                    "resolved": True,
                    "resolution": resolution,
                    "resolved_at": now,
                }
            )
            resolved_count += 1
    if resolved_count:
        _write_conflicts(conflicts, path)
    return resolved_count


def reconcile_file_conflicts_against_disk(
    *,
    folder_id: str,
    active_relpaths: Iterable[str],
    resolution: str = "disappeared",
    path: Path | None = None,
) -> int:
    """Resolve unresolved file records that no longer exist on disk.

    The filesystem (Syncthing's ``*.sync-conflict-*`` copies) is the source of
    truth for whether a file conflict is still live; this ledger is only a
    cache/history over it. A copy can vanish because the user deleted it, a
    peer's resolution propagated, or it was cleaned up out of band — in every
    case the record should stop counting as an open conflict. ``active_relpaths``
    is the set of copies the current scan still sees for this folder.
    """
    active = set(active_relpaths)
    conflicts = read_conflicts(path)
    resolved_count = 0
    now = _timestamp()
    for conflict in conflicts:
        if (
            conflict.get("resolved")
            or conflict.get("kind") != FILE_CONFLICT_KIND
            or conflict.get("folder_id") != folder_id
        ):
            continue
        if str(conflict.get("path") or "") not in active:
            conflict.update(
                {
                    "resolved": True,
                    "resolution": resolution,
                    "resolved_at": now,
                }
            )
            resolved_count += 1
    if resolved_count:
        _write_conflicts(conflicts, path)
    return resolved_count


def resolve_conflict(
    conflict_id: str,
    action: str,
    *,
    path: Path | None = None,
    home: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve one conflict record.

    Branch conflicts: ``keep_local`` keeps the local branch as-is (the peer
    surfaces the same divergence on its side); ``use_remote`` safety-stashes
    the worktree (including untracked files) then hard-resets to the remote
    commit.

    File conflicts: ``keep_local`` restores the losing local copy over the
    file, ``use_remote`` keeps the synced winner; either way the
    ``*.sync-conflict-*`` copy is removed.
    """
    if action not in RESOLVE_ACTIONS:
        raise CodeSyncError(
            f"Unknown resolve action {action!r}; use keep_local or use_remote."
        )
    conflicts = read_conflicts(path)
    conflict = next((item for item in conflicts if item.get("id") == conflict_id), None)
    if conflict is None:
        raise CodeSyncError(f"No conflict with id {conflict_id!r}.")
    if conflict.get("resolved"):
        return conflict

    if conflict.get("kind") == BRANCH_CONFLICT_KIND:
        _resolve_branch_conflict(conflict, action, home, config_path)
    elif conflict.get("kind") == FILE_CONFLICT_KIND:
        _resolve_file_conflict(conflict, action, home, config_path)
    else:
        raise CodeSyncError(f"Unknown conflict kind {conflict.get('kind')!r}.")

    conflict.update(
        {"resolved": True, "resolution": action, "resolved_at": _timestamp()}
    )
    _write_conflicts(conflicts, path)
    return conflict


def _conflict_folder_root(
    conflict: dict[str, Any], home: Path | None, config_path: Path | None
) -> Path:
    folder = folder_for_id(str(conflict.get("folder_id") or ""), config_path)
    if folder is None:
        raise CodeSyncError(
            f"Conflict references unknown sync folder {conflict.get('folder_id')!r}."
        )
    return folder.absolute_path(home)


def _resolve_branch_conflict(
    conflict: dict[str, Any],
    action: str,
    home: Path | None,
    config_path: Path | None,
) -> None:
    if action == "keep_local":
        return
    folder_root = _conflict_folder_root(conflict, home, config_path)
    repo_dir = (folder_root / str(conflict.get("repo_relpath") or "")).resolve()
    if not (repo_dir / ".git").exists():
        raise CodeSyncError(f"No git repository at {repo_dir}.")
    remote_sha = str(conflict.get("remote_sha") or "")
    if not remote_sha:
        raise CodeSyncError("Conflict record is missing remote_sha.")
    # Safety net first: nothing uncommitted is lost by the hard reset.
    subprocess.run(
        [
            "git",
            "stash",
            "push",
            "--include-untracked",
            "-m",
            STASH_BACKUP_MESSAGE,
        ],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    result = subprocess.run(
        ["git", "reset", "--hard", remote_sha],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CodeSyncError(
            f"git reset --hard {remote_sha} failed: {result.stderr.strip()}"
        )


def _resolve_file_conflict(
    conflict: dict[str, Any],
    action: str,
    home: Path | None,
    config_path: Path | None,
) -> None:
    folder_root = _conflict_folder_root(conflict, home, config_path)
    conflict_copy = folder_root / str(conflict.get("path") or "")
    if not conflict_copy.is_file():
        return  # Already cleaned up out of band.
    if action == "keep_local":
        # The sync-conflict copy is the losing local version; restore it.
        original = conflict_copy.with_name(_original_name(conflict_copy.name))
        os.replace(conflict_copy, original)
    else:
        conflict_copy.unlink()


def _original_name(conflict_name: str) -> str:
    """Strip Syncthing's ``.sync-conflict-...`` marker from a filename."""
    stem, marker, remainder = conflict_name.partition(".sync-conflict-")
    if not marker:
        return conflict_name
    suffix_index = remainder.find(".")
    suffix = remainder[suffix_index:] if suffix_index != -1 else ""
    return stem + suffix


def _write_conflicts(conflicts: list[dict[str, Any]], path: Path | None) -> None:
    conflicts_path = path or CODE_SYNC_CONFLICTS_PATH
    conflicts_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "conflicts": conflicts}
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=conflicts_path.parent, delete=False
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, conflicts_path)


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
