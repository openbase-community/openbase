"""Opt-in personal skill sharing through the managed file-sync engine."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from openbase_coder_cli import skills_autolink, sync_config
from openbase_coder_cli.file_lock import LOCK_EX, LOCK_UN, flock
from openbase_coder_cli.paths import OPENBASE_BASE_DIR

STATE_PATH = OPENBASE_BASE_DIR / "skill-sync.json"
PERSONAL_SKILLS = ".agents/skills"
LEGACY_SKILL_FOLDERS = {
    ".openbase/codex_home/skills",
    ".openbase/claude_config/skills",
}


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
    # Existing manually configured skill sharing is already enabled. No
    # preference has been chosen until the user operates the Settings toggle.
    return any(f.relpath == PERSONAL_SKILLS for f in sync_config.sync_folders())


def accepts_peer_folder(relpath: str) -> bool:
    state = _read_state()
    if relpath in LEGACY_SKILL_FOLDERS:
        return False
    if state["enabled"] is not False:
        return True
    return not any(
        relpath == blocked or relpath.startswith(blocked + "/")
        for blocked in [PERSONAL_SKILLS, *state["managed_folders"]]
    )


def _source_folders() -> tuple[list[str], list[str]]:
    home = Path.home().resolve()
    sources = {PERSONAL_SKILLS}
    warnings = []
    for skill in skills_autolink.list_skill_dirs(skills_autolink.home_skills_dir()):
        source = skill.resolve()
        if not source.is_relative_to(home):
            warnings.append(f"{skill.name}: linked source is outside your home folder.")
            continue
        relpath = source.relative_to(home).as_posix()
        if relpath.startswith(PERSONAL_SKILLS + "/"):
            continue
        if (
            relpath == "."
            or relpath.split("/")[0] in {".openbase", ".ssh", ".gnupg"}
            or relpath.startswith((".codex/plugins/", ".claude/plugins/"))
        ):
            warnings.append(f"{skill.name}: machine-local source cannot be synced.")
            continue
        sources.add(sync_config.validate_relpath(relpath))
    return sorted(sources, key=lambda p: (p.count("/"), p)), warnings


def _reconcile(state: dict) -> dict:
    folders = list(sync_config.sync_folders())
    existing = {f.relpath for f in folders}
    managed = set(state["managed_folders"])
    active = state["enabled"]
    if active is None:
        active = PERSONAL_SKILLS in existing
    added: list[str] = []
    removed: list[str] = []
    warnings: list[str] = []
    if active:
        sources, warnings = _source_folders()
        skills_autolink.home_skills_dir().mkdir(parents=True, exist_ok=True)
        for relpath in sources:
            if any(relpath == p or relpath.startswith(p + "/") for p in existing):
                continue
            sync_config.add_sync_folder(relpath)
            existing.add(relpath)
            managed.add(relpath)
            added.append(relpath)
        for relpath in existing & LEGACY_SKILL_FOLDERS:
            sync_config.remove_sync_folder(relpath)
            removed.append(relpath)
    elif state["enabled"] is False:
        for relpath in existing & (managed | {PERSONAL_SKILLS}):
            sync_config.remove_sync_folder(relpath)
            removed.append(relpath)
    if sorted(managed) != state["managed_folders"]:
        state["managed_folders"] = sorted(managed)
        _write_state(state)
    return {"added": added, "removed": removed, "warnings": warnings}


def reconcile() -> dict:
    """Discover new linked sources without changing other folder registrations."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.with_suffix(".lock").open("a") as lock:
        flock(lock, LOCK_EX)
        try:
            return _reconcile(_read_state())
        finally:
            flock(lock, LOCK_UN)


def set_enabled(value: bool) -> dict:
    from openbase_coder_cli.code_sync import manager

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.with_suffix(".lock").open("a") as lock:
        flock(lock, LOCK_EX)
        previous = _read_state()
        previous_config = sync_config.read_sync_config()
        try:
            state = {**previous, "enabled": value}
            _write_state(state)
            result = _reconcile(state)
            if value and not sync_config.code_sync_enabled():
                # Arming an existing transport needs no service changes. A
                # first-time enable uses the same eligibility checks as Sync.
                manager.enable_code_sync()
            elif result["added"] or result["removed"]:
                manager.apply_settings_change()
            return result
        except Exception:
            # Restore the persisted preferences/registrations on failure so
            # a rejected enable cannot silently arm sharing on a later tick.
            _write_state(previous)
            sync_config._write_sync_config(
                previous_config, sync_config.SYNC_CONFIG_PATH
            )
            raise
        finally:
            flock(lock, LOCK_UN)


def settings_payload() -> dict:
    active = enabled()
    _, warnings = _source_folders() if active else ([], [])
    return {
        "sync_skills_across_devices": active,
        "device_sync_enabled": sync_config.code_sync_enabled(),
        "warnings": warnings,
    }
