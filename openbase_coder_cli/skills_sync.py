"""Opt-in personal skill sharing through Openbase Sync.

Sharing means the personal skills directory (``~/.agents/skills``) and the
folders its skills link to are roots of the Openbase Sync daemon, so the
daemon mirrors them to the user's other computers. The preference lives in
``~/.openbase/skill-sync.json``; the roots live in the daemon config.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from openbase_coder_cli import skills_autolink, sync_daemon
from openbase_coder_cli.file_lock import LOCK_EX, LOCK_UN, flock
from openbase_coder_cli.paths import OPENBASE_BASE_DIR

STATE_PATH = OPENBASE_BASE_DIR / "skill-sync.json"
PERSONAL_SKILLS = sync_daemon.PERSONAL_SKILLS_ROOT
# Top-level home folders that hold machine-local state and never sync.
MACHINE_LOCAL_TOP_LEVEL = {".openbase", ".ssh", ".gnupg"}


def _read_state() -> dict:
    if not STATE_PATH.exists():
        return {"schema_version": 1, "enabled": None, "managed_folders": []}
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1:
        raise ValueError("Unsupported skill-sync state version; update Openbase.")
    return state


def _write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=STATE_PATH.parent, delete=False) as out:
        json.dump(state, out, indent=2)
        out.write("\n")
        temp_path = Path(out.name)
    os.replace(temp_path, STATE_PATH)


def enabled() -> bool:
    state = _read_state()
    if state["enabled"] is not None:
        return state["enabled"]
    # No preference chosen yet: sharing is on exactly when the skills folder
    # is already mirrored (for example, added by a migration).
    return sync_daemon.path_is_synced(PERSONAL_SKILLS)


def _linked_sources() -> tuple[list[Path], list[str]]:
    home = Path.home().resolve()
    personal = sync_daemon.expand_root_path(PERSONAL_SKILLS)
    sources: dict[Path, None] = {}
    warnings = []
    for skill in skills_autolink.list_skill_dirs(skills_autolink.home_skills_dir()):
        source = skill.resolve()
        if not source.is_relative_to(home):
            warnings.append(f"{skill.name}: linked source is outside your home folder.")
            continue
        if source == personal or source.is_relative_to(personal):
            continue
        relpath = source.relative_to(home).as_posix()
        if (
            relpath == "."
            or relpath.split("/")[0] in MACHINE_LOCAL_TOP_LEVEL
            or relpath.startswith((".codex/plugins/", ".claude/plugins/"))
        ):
            warnings.append(f"{skill.name}: machine-local source cannot be synced.")
            continue
        sources[source] = None
    ordered = sorted(sources, key=lambda p: (len(p.parts), str(p)))
    return ordered, warnings


def linked_source_paths() -> list[Path]:
    """Folders outside ``~/.agents/skills`` that personal skills link to."""
    return _linked_sources()[0]


def _wanted_roots() -> tuple[list[str], list[str]]:
    sources, warnings = _linked_sources()
    roots = [PERSONAL_SKILLS]
    for source in sources:
        candidate = sync_daemon.home_relative_root_path(source)
        if candidate not in roots:
            roots.append(candidate)
    return roots, warnings


def _apply(state: dict) -> dict:
    """Bring the daemon roots in line with the preference.

    Only roots this module added are ever removed. Nothing changes when Openbase
    Sync is not configured on this computer; the preference is kept and applied
    by the next toggle (or a migration's product folders).
    """
    result = {"added": [], "removed": [], "warnings": [], "restarted": False}
    if not sync_daemon.is_configured():
        if state["enabled"]:
            result["warnings"].append(
                "Openbase Sync is not set up on this computer; skills will "
                "sync once it is."
            )
        return result
    managed = {_as_root_path(path) for path in state["managed_folders"]}
    if state["enabled"]:
        wanted, result["warnings"] = _wanted_roots()
        skills_autolink.home_skills_dir().mkdir(parents=True, exist_ok=True)
        change = sync_daemon.add_roots(wanted)
        result["added"] = [root["path"] for root in change.added]
        managed.update(result["added"])
        result["warnings"] += [
            f"{path}: {reason}"
            for path, reason in change.skipped
            if not reason.startswith("already inside")
        ]
    elif state["enabled"] is False:
        removed = sync_daemon.remove_roots(managed | {PERSONAL_SKILLS})
        result["removed"] = [root["path"] for root in removed]
        managed.clear()
    if sorted(managed) != state["managed_folders"]:
        state["managed_folders"] = sorted(managed)
        _write_state(state)
    if result["added"] or result["removed"]:
        result["restarted"] = sync_daemon.restart_service_if_installed()
    return result


def _as_root_path(path: str) -> str:
    # Older releases recorded home-relative folder paths ("Developer/skills").
    return path if path.startswith(("~", "/")) else "~/" + path


def reconcile() -> dict:
    """Add roots for newly linked skill sources (when sharing is on)."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.with_suffix(".lock").open("a") as lock:
        flock(lock, LOCK_EX)
        try:
            return _apply(_read_state())
        finally:
            flock(lock, LOCK_UN)


def set_enabled(value: bool) -> dict:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.with_suffix(".lock").open("a") as lock:
        flock(lock, LOCK_EX)
        previous = _read_state()
        try:
            state = {**previous, "enabled": value}
            _write_state(state)
            return _apply(state)
        except Exception:
            # A failed apply must not leave the preference armed for later.
            _write_state(previous)
            raise
        finally:
            flock(lock, LOCK_UN)


def settings_payload() -> dict:
    active = enabled()
    _, warnings = _wanted_roots() if active else ([], [])
    return {
        "sync_skills_across_devices": active,
        # Whether Openbase Sync is set up here; the console explains that
        # shared skills wait for it when this is False.
        "device_sync_enabled": sync_daemon.is_configured(),
        "warnings": warnings,
    }
