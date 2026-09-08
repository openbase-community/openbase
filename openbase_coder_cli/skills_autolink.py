"""Auto-link personal agent skills into the shared agent homes.

When ``auto_link_personal_skills`` is enabled in the dispatcher config,
skills under ``~/.agents/skills`` (and Claude-installed skills) are symlinked
into the shared ``~/.codex/skills`` and ``~/.claude/skills`` so both backends
discover them.

Standalone (no Django imports) so it can run from the Django app startup, the
skills API, and the ``openbase-coder routines run-loop`` service, which
re-syncs periodically so newly added skills appear without a restart.
"""

from __future__ import annotations

from pathlib import Path

from openbase_coder_cli import dispatcher_config
from openbase_coder_cli.paths import (
    CLAUDE_CONFIG_DIR,
    CODEX_HOME_DIR,
)


def home_skills_dir() -> Path:
    return Path.home() / ".agents" / "skills"


def claude_skills_dir() -> Path:
    return CLAUDE_CONFIG_DIR / "skills"


def personal_skill_source_dirs() -> dict[str, Path]:
    return {
        "home": home_skills_dir(),
        "claude": claude_skills_dir(),
    }


def auto_link_target_dirs() -> dict[str, Path]:
    return {
        "codex": CODEX_HOME_DIR / "skills",
        "claude": claude_skills_dir(),
    }


def list_skill_dirs(skills_root: Path) -> list[Path]:
    if not skills_root.is_dir():
        return []
    return sorted(
        child
        for child in skills_root.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    )


def link_skill_dir(source_dir: Path, target_dir: Path) -> bool:
    """Symlink a skill dir into a target root.

    Returns True when a new link was created, False when the link already
    points at the source. Raises FileExistsError for a conflicting entry.
    """
    if target_dir.exists() or target_dir.is_symlink():
        if target_dir.is_symlink() and _same_resolved_path(target_dir, source_dir):
            return False
        raise FileExistsError(
            f"Skill '{target_dir.name}' already exists in {target_dir.parent}"
        )
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    target_dir.symlink_to(source_dir, target_is_directory=True)
    return True


def sync_auto_linked_skills() -> dict:
    """Link every personal skill into both shared agent homes.

    No-op (with ``enabled: False``) when the auto-link setting is off.
    """
    enabled = dispatcher_config.auto_link_personal_skills()
    results: list[dict] = []
    created = 0
    already_linked = 0
    conflicts = 0
    errors = 0

    if enabled:
        for source_scope, source_root in personal_skill_source_dirs().items():
            skill_dirs = list_skill_dirs(source_root)
            for target_scope, target_root in auto_link_target_dirs().items():
                if target_root == source_root:
                    continue
                for source_dir in skill_dirs:
                    target_dir = target_root / source_dir.name
                    entry = {
                        "name": source_dir.name,
                        "source_scope": source_scope,
                        "target_scope": target_scope,
                        "source_dir": str(source_dir),
                        "target_dir": str(target_dir),
                    }
                    try:
                        if link_skill_dir(source_dir, target_dir):
                            created += 1
                            entry["status"] = "linked"
                            entry["created"] = True
                        else:
                            already_linked += 1
                            entry["status"] = "already_linked"
                            entry["created"] = False
                    except FileExistsError as exc:
                        conflicts += 1
                        entry["status"] = "conflict"
                        entry["error"] = str(exc)
                    except OSError as exc:
                        errors += 1
                        entry["status"] = "error"
                        entry["error"] = str(exc)
                    results.append(entry)

    return {
        "enabled": enabled,
        "created": created,
        "already_linked": already_linked,
        "conflicts": conflicts,
        "errors": errors,
        "results": results,
    }


def _same_resolved_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=True) == right.resolve(strict=True)
    except FileNotFoundError:
        return left.resolve(strict=False) == right.resolve(strict=False)
