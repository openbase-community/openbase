"""Project attribution for loops (routines).

Routine state (in super-agents) knows only a free-form ``cwd``. Openbase's
product notion of a *project* is the tracked project registry
(``coder-projects.json`` in the data dir), so the join is derived here on the
read side: a loop belongs to the tracked project whose path is the longest
prefix of its ``cwd``. Nothing is persisted, so existing routine state needs
no migration and the attribution follows the registry as projects come and go.

This lives in the Openbase CLI, not in super-agents: projects are an Openbase
product concept and super-agents must stay usable without them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openbase_coder_cli.thread_sync.projects import get_recent_projects

PROJECT_PATH_KEY = "projectPath"


def _normalize(directory: str) -> Path | None:
    try:
        return Path(directory).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def tracked_project_for_directory(
    directory: str | None,
    projects: list[dict[str, Any]] | None = None,
) -> str | None:
    """Return the tracked project path that contains ``directory``, if any.

    The deepest (longest) tracked project wins, so a loop whose cwd is a
    workspace inside a tracked parent folder is attributed to the workspace.
    Comparison is on resolved, case-folded paths (macOS is case-insensitive);
    the returned value is the registry's own spelling of the path.
    """
    if not directory:
        return None
    target = _normalize(directory)
    if target is None:
        return None
    target_key = str(target).casefold()
    candidates = projects if projects is not None else get_recent_projects()

    best: str | None = None
    best_len = -1
    for project in candidates:
        raw = project.get("path")
        if not isinstance(raw, str) or not raw:
            continue
        root = _normalize(raw)
        if root is None:
            continue
        root_key = str(root).casefold()
        if target_key != root_key and not target_key.startswith(
            root_key.rstrip("/") + "/"
        ):
            continue
        if len(root_key) > best_len:
            best, best_len = raw, len(root_key)
    return best


def annotate_routine_project(
    routine: dict[str, Any],
    projects: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Stamp ``projectPath`` on one routine dict (in place) and return it."""
    routine[PROJECT_PATH_KEY] = tracked_project_for_directory(
        routine.get("cwd"), projects
    )
    return routine


def annotate_routines_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Stamp ``projectPath`` on every routine in a list or detail payload.

    Loads the registry once per call. Peer (fleet) items are left alone: the
    owning device stamps its own loops against its own registry.
    """
    projects = get_recent_projects()
    if isinstance(payload.get("routines"), list):
        for routine in payload["routines"]:
            if isinstance(routine, dict) and "origin_host" not in routine:
                annotate_routine_project(routine, projects)
    routine = payload.get("routine")
    if isinstance(routine, dict):
        annotate_routine_project(routine, projects)
    return payload


def filter_routines_by_project(
    payload: dict[str, Any], project_path: str
) -> dict[str, Any]:
    """Keep only routines attributed to ``project_path`` (path-normalized)."""
    wanted = _normalize(project_path)
    wanted_key = str(wanted).casefold() if wanted else project_path.casefold()

    def matches(routine: dict[str, Any]) -> bool:
        stamped = routine.get(PROJECT_PATH_KEY)
        if not isinstance(stamped, str):
            return False
        resolved = _normalize(stamped)
        key = str(resolved).casefold() if resolved else stamped.casefold()
        return key == wanted_key

    routines = [r for r in payload.get("routines") or [] if matches(r)]
    filtered = dict(payload)
    filtered["routines"] = routines
    filtered["count"] = len(routines)
    return filtered
