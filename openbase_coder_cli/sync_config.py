"""Schema-versioned config for the code-sync subsystem.

``~/.openbase/sync-config.json`` is product state (same refuse-newer
semantics as ``dispatcher-config.json``): whether code sync is enabled, the
home-relative folders being synced, and the write-lease policy. Folder
identity is the home-relative path so every device mounts the same folder ID
at ``$HOME/<relpath>`` regardless of the local home directory name.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from openbase_coder_cli.paths import SYNC_CONFIG_PATH

# Bump alongside a forward-only migration; see the workspace AUTO_UPDATE.md.
SCHEMA_VERSION_KEY = "schema_version"
SYNC_CONFIG_SCHEMA_VERSION = 1
ENABLED_KEY = "enabled"
FOLDERS_KEY = "folders"
LEASE_MODE_KEY = "lease_mode"
LEASE_HOLDER_KEY = "lease_holder_device_id"
LEASE_MODES = ("auto", "manual")
DEFAULT_LEASE_MODE = "auto"
FOLDER_ID_PREFIX = "cs-"
FOLDER_ID_HEX_DIGITS = 16

# Product state that syncs between a user's devices as a first-class part
# of code sync: the thread-sync transport. It is the only path allowed
# inside ~/.openbase — everything else there is machine-local (auth, device
# identity, databases, logs, packages) and must never sync. The shared agent
# homes (~/.codex, ~/.claude) are the user's own and never sync either;
# skill symlinks in them are machine-local and self-heal at service start.
PRODUCT_STATE_RELPATHS = (
    ".openbase/thread-sync",
)


@dataclass(frozen=True)
class SyncFolder:
    """One synced directory, identified by its home-relative path."""

    relpath: str
    extra_ignores: tuple[str, ...] = ()

    @property
    def folder_id(self) -> str:
        return folder_id_for_relpath(self.relpath)

    def absolute_path(self, home: Path | None = None) -> Path:
        return (home or Path.home()) / self.relpath

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.folder_id,
            "relpath": self.relpath,
            "extra_ignores": list(self.extra_ignores),
        }


def folder_id_for_relpath(relpath: str) -> str:
    """Deterministic folder ID shared by every device syncing ``relpath``."""
    digest = hashlib.sha256(relpath.encode("utf-8")).hexdigest()
    return FOLDER_ID_PREFIX + digest[:FOLDER_ID_HEX_DIGITS]


def validate_relpath(relpath: str) -> str:
    """Validate and normalize a home-relative sync folder path."""
    if not isinstance(relpath, str):
        raise ValueError("Sync folder path must be a string.")
    normalized = relpath.strip().strip("/")
    if not normalized:
        raise ValueError("Sync folder path cannot be empty.")
    if relpath.strip().startswith(("/", "~")) or Path(normalized).is_absolute():
        raise ValueError(
            "Sync folder paths are home-relative (e.g. 'Projects/myapp'), not absolute."
        )
    parts = PurePosixPath(normalized).parts
    if any(part == ".." for part in parts):
        raise ValueError("Sync folder paths cannot contain '..'.")
    canonical = str(PurePosixPath(*parts))
    if parts[0] == ".openbase" and canonical not in PRODUCT_STATE_RELPATHS:
        raise ValueError(
            "Folders inside ~/.openbase cannot be synced (machine-local state)."
        )
    return canonical


def relpath_for_path(path: Path | str) -> str:
    """Convert an absolute path under ``$HOME`` to a validated relpath."""
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        return validate_relpath(str(resolved))
    try:
        relative = resolved.relative_to(Path.home())
    except ValueError:
        raise ValueError(
            f"Only paths under your home directory can be synced: {resolved}"
        ) from None
    return validate_relpath(str(relative))


def read_sync_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or SYNC_CONFIG_PATH
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    found_version = int(payload.get(SCHEMA_VERSION_KEY, 1) or 1)
    if found_version > SYNC_CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"{config_path.name} schema {found_version} was written by a "
            "newer Openbase Coder; update the CLI."
        )
    return payload


def code_sync_enabled(path: Path | None = None) -> bool:
    return read_sync_config(path).get(ENABLED_KEY) is True


def set_code_sync_enabled(enabled: bool, path: Path | None = None) -> Path:
    config_path = path or SYNC_CONFIG_PATH
    payload = {**read_sync_config(config_path), ENABLED_KEY: bool(enabled)}
    _write_sync_config(payload, config_path)
    return config_path


def lease_mode(path: Path | None = None) -> str:
    value = read_sync_config(path).get(LEASE_MODE_KEY)
    return (
        value
        if isinstance(value, str) and value in LEASE_MODES
        else (DEFAULT_LEASE_MODE)
    )


def set_lease_mode(mode: str, path: Path | None = None) -> Path:
    normalized = mode.strip().lower()
    if normalized not in LEASE_MODES:
        allowed = ", ".join(LEASE_MODES)
        raise ValueError(f"Lease mode must be one of: {allowed}.")
    config_path = path or SYNC_CONFIG_PATH
    payload = {**read_sync_config(config_path), LEASE_MODE_KEY: normalized}
    _write_sync_config(payload, config_path)
    return config_path


def lease_holder_device_id(path: Path | None = None) -> str | None:
    value = read_sync_config(path).get(LEASE_HOLDER_KEY)
    return value if isinstance(value, str) and value else None


def set_lease_holder_device_id(device_id: str | None, path: Path | None = None) -> Path:
    config_path = path or SYNC_CONFIG_PATH
    payload = {**read_sync_config(config_path), LEASE_HOLDER_KEY: device_id or ""}
    _write_sync_config(payload, config_path)
    return config_path


def sync_folders(path: Path | None = None) -> tuple[SyncFolder, ...]:
    raw_folders = read_sync_config(path).get(FOLDERS_KEY)
    if not isinstance(raw_folders, list):
        return ()
    folders: list[SyncFolder] = []
    for entry in raw_folders:
        if not isinstance(entry, dict):
            continue
        relpath = entry.get("relpath")
        if not isinstance(relpath, str) or not relpath:
            continue
        extra_ignores = tuple(
            pattern
            for pattern in entry.get("extra_ignores", [])
            if isinstance(pattern, str) and pattern.strip()
        )
        folders.append(SyncFolder(relpath=relpath, extra_ignores=extra_ignores))
    return tuple(folders)


def set_sync_folders(
    folders: list[SyncFolder | dict[str, Any]], path: Path | None = None
) -> tuple[SyncFolder, ...]:
    """Replace the synced folder list (full-list replace semantics)."""
    validated: list[SyncFolder] = []
    seen: set[str] = set()
    for entry in folders:
        if isinstance(entry, SyncFolder):
            relpath = entry.relpath
            extra_ignores = entry.extra_ignores
        else:
            relpath = entry.get("relpath", "")
            extra_ignores = tuple(
                pattern
                for pattern in entry.get("extra_ignores", [])
                if isinstance(pattern, str) and pattern.strip()
            )
        normalized = validate_relpath(relpath)
        if normalized in seen:
            continue
        seen.add(normalized)
        validated.append(SyncFolder(relpath=normalized, extra_ignores=extra_ignores))

    config_path = path or SYNC_CONFIG_PATH
    payload = {
        **read_sync_config(config_path),
        FOLDERS_KEY: [
            {"relpath": folder.relpath, "extra_ignores": list(folder.extra_ignores)}
            for folder in validated
        ],
    }
    _write_sync_config(payload, config_path)
    return tuple(validated)


def add_sync_folder(relpath: str, path: Path | None = None) -> SyncFolder:
    normalized = validate_relpath(relpath)
    existing = list(sync_folders(path))
    if all(folder.relpath != normalized for folder in existing):
        existing.append(SyncFolder(relpath=normalized))
    set_sync_folders(existing, path)
    return SyncFolder(relpath=normalized)


def remove_sync_folder(relpath: str, path: Path | None = None) -> bool:
    normalized = validate_relpath(relpath)
    existing = list(sync_folders(path))
    remaining = [folder for folder in existing if folder.relpath != normalized]
    if len(remaining) == len(existing):
        return False
    set_sync_folders(remaining, path)
    return True


def folder_for_id(folder_id: str, path: Path | None = None) -> SyncFolder | None:
    for folder in sync_folders(path):
        if folder.folder_id == folder_id:
            return folder
    return None


def folder_for_relpath(relpath: str, path: Path | None = None) -> SyncFolder | None:
    normalized = validate_relpath(relpath)
    for folder in sync_folders(path):
        if folder.relpath == normalized:
            return folder
    return None


def add_folder_ignore(
    relpath: str, pattern: str, path: Path | None = None
) -> SyncFolder:
    """Append a custom Syncthing ignore pattern to a synced folder.

    Patterns render under the ``// Folder-specific ignores`` section of the
    managed ``.stignore`` (see code_sync.ignores); duplicates are ignored.
    """
    cleaned = pattern.strip()
    if not cleaned:
        raise ValueError("Ignore pattern cannot be empty.")
    normalized = validate_relpath(relpath)
    folders = list(sync_folders(path))
    target = next((f for f in folders if f.relpath == normalized), None)
    if target is None:
        raise ValueError(f"~/{normalized} is not a synced folder.")
    if cleaned in target.extra_ignores:
        return target
    updated = SyncFolder(
        relpath=normalized, extra_ignores=(*target.extra_ignores, cleaned)
    )
    set_sync_folders([updated if f.relpath == normalized else f for f in folders], path)
    return updated


def remove_folder_ignore(relpath: str, pattern: str, path: Path | None = None) -> bool:
    """Remove a custom ignore pattern from a folder; True if one was removed."""
    cleaned = pattern.strip()
    normalized = validate_relpath(relpath)
    folders = list(sync_folders(path))
    target = next((f for f in folders if f.relpath == normalized), None)
    if target is None or cleaned not in target.extra_ignores:
        return False
    updated = SyncFolder(
        relpath=normalized,
        extra_ignores=tuple(p for p in target.extra_ignores if p != cleaned),
    )
    set_sync_folders([updated if f.relpath == normalized else f for f in folders], path)
    return True


def _write_sync_config(payload: dict[str, Any], config_path: Path) -> None:
    payload = {**payload, SCHEMA_VERSION_KEY: SYNC_CONFIG_SCHEMA_VERSION}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=config_path.parent, delete=False
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, config_path)
