"""Openbase project projection cache derived from Codex thread directories."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from openbase_coder_cli.multi_config import multi_repo_name_set
from openbase_coder_cli.thread_sync.thread_sync_common import translate_home_path

PROJECTS_FILE = Path.home() / ".openbase" / "coder-projects.json"
IGNORED_PROJECT_ROOTS = (Path("/private"), Path("/var"))
IGNORED_EXACT_PROJECT_PATHS = (Path.home(),)


def _load_projects() -> list[dict]:
    """Load projects from the JSON file."""
    if not PROJECTS_FILE.exists():
        return []
    with open(PROJECTS_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        return []
    projects: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        project = dict(item)
        path = project.get("path")
        if isinstance(path, str):
            project["path"] = translate_home_path(path) or path
        projects.append(project)
    return projects


def _save_projects(projects: list[dict]) -> None:
    """Save projects to the JSON file."""
    PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(projects, f, indent=2)


def _dedup_key(directory: str) -> str:
    """Return a case-folded resolved path for deduplication.

    macOS has a case-insensitive filesystem, so paths differing only by
    case refer to the same directory.
    """
    return str(Path(directory).resolve()).casefold()


def _is_ignored_project_path(directory: str) -> bool:
    """Return whether a path should be hidden from recent projects."""
    try:
        resolved = Path(directory).expanduser().resolve()
    except OSError:
        return False
    if any(
        resolved == path.expanduser().resolve() for path in IGNORED_EXACT_PROJECT_PATHS
    ):
        return True
    return any(
        resolved == root or root in resolved.parents for root in IGNORED_PROJECT_ROOTS
    )


def project_root_for_thread_directory(directory: str) -> str:
    """Return the Openbase project root represented by a Codex thread cwd."""
    resolved = Path(directory).expanduser().resolve()
    if (resolved / "multi.json").is_file():
        return str(resolved)

    candidates = [resolved, *resolved.parents]
    for candidate in candidates:
        repo_names = multi_repo_name_set(candidate)
        if not repo_names:
            continue
        try:
            relative = resolved.relative_to(candidate)
        except ValueError:
            continue
        if not relative.parts or relative.parts[0] in repo_names:
            return str(candidate)
    return str(resolved)


def _upsert_project(
    projects: list[dict],
    directory: str,
    *,
    source: str,
    clear_hidden: bool,
) -> list[dict]:
    if _is_ignored_project_path(directory):
        return projects

    key = _dedup_key(directory)
    now = datetime.now().isoformat()
    existing = next((p for p in projects if _dedup_key(p.get("path", "")) == key), None)
    if existing and existing.get("hidden") and not clear_hidden:
        return projects

    projects = [p for p in projects if _dedup_key(p.get("path", "")) != key]
    entry = {
        "path": directory,
        "last_worked_on": now,
        "source": source,
    }
    if existing and existing.get("source") == "manual":
        entry["source"] = "manual"
    projects.insert(0, entry)
    return projects


def track_project(directory: str) -> None:
    """Track a project as recently used.

    Args:
        directory: The absolute path to the project directory.
    """
    try:
        project_path = project_root_for_thread_directory(directory)
    except OSError:
        return
    projects = _load_projects()
    projects = _upsert_project(
        projects,
        project_path,
        source="manual",
        clear_hidden=True,
    )
    if projects:
        _save_projects(projects)


def refresh_projects_from_thread_directories(directories: list[str]) -> None:
    """Refresh the Openbase project cache from Codex thread cwd values."""
    projects = _load_projects()
    for directory in reversed(directories):
        if not directory:
            continue
        try:
            project_path = project_root_for_thread_directory(directory)
        except OSError:
            continue
        projects = _upsert_project(
            projects,
            project_path,
            source="thread",
            clear_hidden=False,
        )
    _save_projects(projects)


def remove_project(directory: str) -> bool:
    """Remove a project from the recent projects list only.

    Args:
        directory: The project directory path to stop tracking.

    Returns:
        True when at least one tracked entry was removed.
    """
    try:
        project_path = project_root_for_thread_directory(directory)
    except OSError:
        project_path = directory
    key = _dedup_key(project_path)
    projects = _load_projects()
    removed = False
    kept_projects: list[dict] = []
    for project in projects:
        if _dedup_key(project.get("path", "")) != key:
            kept_projects.append(project)
            continue
        removed = True
        kept_projects.append(
            {
                **project,
                "hidden": True,
                "last_worked_on": datetime.now().isoformat(),
            }
        )
    if not removed:
        return False
    _save_projects(kept_projects)
    return True


def get_recent_projects() -> list[dict]:
    """Get list of recent projects, ordered by most recently worked on.

    Returns:
        List of project dicts with "path" key only (timestamps excluded).
        Duplicate paths (differing only by case on macOS) are collapsed.
    """
    projects = _load_projects()
    seen: set[str] = set()
    result: list[dict] = []
    for p in projects:
        if p.get("hidden"):
            continue
        if _is_ignored_project_path(p["path"]):
            continue
        key = _dedup_key(p["path"])
        if key not in seen:
            seen.add(key)
            result.append({"path": p["path"]})
    return result
