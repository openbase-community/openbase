"""Move a computer from the previous Syncthing-based code sync to Openbase Sync.

The previous sync ran a ``code-sync`` service (a managed Syncthing) with its
engine home in ``~/.openbase/code-sync``, a version store in
``~/.openbase/sync-versions`` and its folder list in
``~/.openbase/sync-config.json``. This module plans and applies the switch:

- stop and uninstall the ``code-sync`` service;
- move those three paths into ``~/.openbase/trash/syncthing-migration-<ts>/``
  (trash, never delete);
- turn the old folders into Openbase Sync roots (``~/Projects``, the thread
  exchange, skills folders), added to the daemon config when it exists;
- optionally move the old engine's ``.stfolder``/``.stignore`` markers from
  the folder roots into the same trash folder.

Planning never changes anything. Applying is idempotent: once the old paths
are in the trash, a second run finds nothing left to do.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from openbase_coder_cli import sync_daemon
from openbase_coder_cli.paths import OPENBASE_BASE_DIR

LEGACY_SERVICE_NAME = "code-sync"
TRASH_PREFIX = "syncthing-migration-"
# Files the old engine kept in every synced folder root.
LEGACY_MARKER_NAMES = (".stfolder", ".stignore", ".stglobalignore")
# The only ``~/.openbase`` folder the old sync mirrored (thread exchange).
THREAD_SYNC_RELPATH = ".openbase/thread-sync"
# Skill folders inside agent homes that an older release synced; never roots.
LEGACY_SKILL_RELPATHS = {
    ".openbase/codex_home/skills",
    ".openbase/claude_config/skills",
}


def legacy_config_path() -> Path:
    return OPENBASE_BASE_DIR / "sync-config.json"


def legacy_engine_dir() -> Path:
    return OPENBASE_BASE_DIR / "code-sync"


def legacy_versions_dir() -> Path:
    return OPENBASE_BASE_DIR / "sync-versions"


def trash_dir() -> Path:
    return OPENBASE_BASE_DIR / "trash"


@dataclass(frozen=True)
class LegacyFolder:
    relpath: str
    extra_ignores: tuple[str, ...] = ()


@dataclass(frozen=True)
class LegacyConfig:
    enabled: bool = False
    folders: tuple[LegacyFolder, ...] = ()
    error: str = ""


def read_legacy_config(path: Path | None = None) -> LegacyConfig:
    """The old ``sync-config.json``; tolerant of a missing or broken file."""
    path = path or legacy_config_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LegacyConfig()
    except (OSError, ValueError) as exc:
        return LegacyConfig(error=f"{path} is unreadable: {exc}")
    if not isinstance(payload, dict):
        return LegacyConfig(error=f"{path} is not a JSON object")
    folders: list[LegacyFolder] = []
    for entry in payload.get("folders") or []:
        if not isinstance(entry, dict):
            continue
        relpath = str(entry.get("relpath") or "").strip().strip("/")
        if not relpath or relpath.startswith(("~", "/")) or ".." in relpath.split("/"):
            continue
        ignores = tuple(
            str(pattern)
            for pattern in entry.get("extra_ignores") or []
            if isinstance(pattern, str)
            and pattern.strip()
            and not pattern.strip().startswith("//")
        )
        folders.append(LegacyFolder(relpath=relpath, extra_ignores=ignores))
    return LegacyConfig(enabled=payload.get("enabled") is True, folders=tuple(folders))


def roots_for_legacy_config(config: LegacyConfig) -> list[str]:
    """Openbase Sync roots (``~/...``) equivalent to the old folder list.

    Machine-local ``~/.openbase`` folders other than the thread exchange are
    dropped, and a folder inside another listed folder is folded into it (roots
    must not nest). An enabled old sync always mirrored the thread exchange, so
    it becomes a root even when the file does not list it.
    """
    relpaths: list[str] = []
    for folder in config.folders:
        relpath = folder.relpath
        if relpath in LEGACY_SKILL_RELPATHS:
            continue
        if relpath.split("/")[0] == ".openbase" and relpath != THREAD_SYNC_RELPATH:
            continue
        if relpath not in relpaths:
            relpaths.append(relpath)
    if config.enabled and THREAD_SYNC_RELPATH not in relpaths:
        relpaths.append(THREAD_SYNC_RELPATH)
    outer = [
        relpath
        for relpath in relpaths
        if not any(
            other != relpath and relpath.startswith(other + "/") for other in relpaths
        )
    ]
    return ["~/" + relpath for relpath in outer]


@dataclass
class MigrationPlan:
    service_installed: bool
    legacy_config: LegacyConfig
    trash_paths: list[Path]
    markers: list[Path]
    roots: list[str]
    daemon_configured: bool
    roots_after: list[dict[str, Any]] = field(default_factory=list)
    root_change: sync_daemon.RootChange = field(default_factory=sync_daemon.RootChange)

    @property
    def dropped_ignore_rules(self) -> int:
        return sum(len(folder.extra_ignores) for folder in self.legacy_config.folders)

    @property
    def has_legacy_state(self) -> bool:
        return bool(self.service_installed or self.trash_paths or self.markers)

    @property
    def nothing_to_do(self) -> bool:
        return not self.has_legacy_state and not self.root_change.changed


def legacy_service_installed() -> bool:
    from openbase_coder_cli.services.definitions import retired_service_stub
    from openbase_coder_cli.services.launchd import (
        _plist_path,
        _wrapper_path,
        launchctl_status,
    )

    stub = retired_service_stub(LEGACY_SERVICE_NAME)
    try:
        if launchctl_status(stub).get("installed"):
            return True
    except Exception:  # noqa: BLE001 - fall back to the generated files
        pass
    return _plist_path(stub).exists() or _wrapper_path(stub).exists()


def _legacy_markers(config: LegacyConfig, home: Path) -> list[Path]:
    markers: list[Path] = []
    for folder in config.folders:
        root = home / folder.relpath
        for name in LEGACY_MARKER_NAMES:
            candidate = root / name
            if candidate.exists() or candidate.is_symlink():
                markers.append(candidate)
    return markers


def plan_migration(
    *, replace_nested: bool = False, include_markers: bool = False
) -> MigrationPlan:
    """What a migration would do on this computer. Changes nothing."""
    config = read_legacy_config()
    trash_paths = [
        path
        for path in (legacy_engine_dir(), legacy_versions_dir(), legacy_config_path())
        if path.exists() or path.is_symlink()
    ]
    roots = roots_for_legacy_config(config)
    configured = sync_daemon.is_configured()
    plan = MigrationPlan(
        service_installed=legacy_service_installed(),
        legacy_config=config,
        trash_paths=trash_paths,
        markers=_legacy_markers(config, Path.home()) if include_markers else [],
        roots=roots,
        daemon_configured=configured,
    )
    if configured:
        plan.roots_after, plan.root_change = sync_daemon.plan_root_additions(
            sync_daemon.configured_roots(), roots, replace_nested=replace_nested
        )
    return plan


@dataclass
class MigrationResult:
    service_removed: bool = False
    trash_folder: Path | None = None
    moved: list[tuple[Path, Path]] = field(default_factory=list)
    roots_written: bool = False
    daemon_restarted: bool = False


def _unique_trash_folder(now: datetime) -> Path:
    base = trash_dir() / f"{TRASH_PREFIX}{now.strftime('%Y%m%d-%H%M%S')}"
    candidate, counter = base, 1
    while candidate.exists():
        counter += 1
        candidate = base.with_name(f"{base.name}-{counter}")
    return candidate


def _trash_destination(path: Path, folder: Path) -> Path:
    """Keep the home-relative layout inside the trash folder."""
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        relative = Path(*path.parts[1:])
    return folder / relative


def apply_migration(
    plan: MigrationPlan, *, restart: bool = True, now: datetime | None = None
) -> MigrationResult:
    """Carry out ``plan``. Safe to call again; it only acts on what exists."""
    from openbase_coder_cli.services.definitions import retired_service_stub
    from openbase_coder_cli.services.launchd import remove_service

    result = MigrationResult()
    # Stop the old engine before moving its home out from under it.
    if plan.service_installed:
        result.service_removed = remove_service(
            retired_service_stub(LEGACY_SERVICE_NAME)
        )

    to_move = [*plan.trash_paths, *plan.markers]
    if to_move:
        folder = _unique_trash_folder(now or datetime.now())
        for path in to_move:
            if not (path.exists() or path.is_symlink()):
                continue
            destination = _trash_destination(path, folder)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
            result.moved.append((path, destination))
        if result.moved:
            result.trash_folder = folder

    if plan.daemon_configured and plan.root_change.changed:
        sync_daemon.set_roots(plan.roots_after)
        result.roots_written = True
        if restart:
            result.daemon_restarted = sync_daemon.restart_service_if_installed()
    return result


def configure_command_hint(roots: list[str]) -> str:
    """The ``sync-daemon configure`` invocation for an unconfigured computer."""
    root_flags = " ".join(f"--root {root}" for root in roots)
    return (
        "openbase-coder sync-daemon configure --role hub --listen <this "
        f"computer's Openbase VPN address> {root_flags}".rstrip()
        + "\n  (on the other computer: --role edge --peer <hub address> "
        "--pair-secret <secret printed by the hub> with the same roots)"
    )
