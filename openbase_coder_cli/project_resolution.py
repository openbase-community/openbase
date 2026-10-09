"""Resolve a spoken or typed project name to its directory.

A dispatcher asked to "start a Super Agent in tic-tac-toe" passed its own
directory as the agent's cwd, so the agent's relative paths failed
(2026-10-09). Resolution is deterministic here instead of left to the
model: a name matches the computer's project list and the immediate
directories of the projects roots, ignoring case and treating spaces,
dashes and underscores alike ("tic tac toe" finds ``tic-tac-toe``). An
unknown or ambiguous name is an error that lists the candidates, never a
silent fallback to some root.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PROJECTS_DIR_ENV = "OPENBASE_CODER_PROJECTS_DIR"


@dataclass(frozen=True)
class ProjectResolution:
    path: str | None = None
    error: str | None = None
    candidates: list[str] = field(default_factory=list)


def normalize_project_name(name: str) -> str:
    return re.sub(r"[\s_\-]+", "-", name.strip().casefold()).strip("-")


def project_roots() -> list[Path]:
    """Where projects live: the configured projects dir (cloud workspaces use
    /data/workspace), the caller's directory, and home."""
    roots = []
    configured = os.environ.get(PROJECTS_DIR_ENV)
    if configured:
        roots.append(Path(configured).expanduser())
    roots.extend([Path.cwd(), Path.home()])
    return roots


def known_project_paths() -> list[str]:
    from openbase_coder_cli.thread_sync.projects import get_recent_projects

    return [project["path"] for project in get_recent_projects()]


def resolve_project_dir(
    name: str,
    *,
    projects: list[str] | None = None,
    roots: list[Path] | None = None,
) -> ProjectResolution:
    raw = name.strip()
    if not raw:
        return ProjectResolution(error="A project name is required.")
    explicit = Path(raw).expanduser()
    if explicit.is_absolute() and explicit.is_dir():
        return ProjectResolution(path=str(explicit.resolve()))

    wanted = normalize_project_name(raw)
    candidates: dict[str, str] = {}
    for path in projects if projects is not None else known_project_paths():
        if Path(path).is_dir():
            candidates.setdefault(str(Path(path).resolve()), Path(path).name)
    for root in roots if roots is not None else project_roots():
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                candidates.setdefault(str(child.resolve()), child.name)

    matches = sorted(path for path, base in candidates.items() if normalize_project_name(base) == wanted)
    if len(matches) == 1:
        return ProjectResolution(path=matches[0])
    if matches:
        return ProjectResolution(
            error=f"More than one project is named {raw!r}; ask which one.",
            candidates=matches,
        )
    return ProjectResolution(
        error=f"No project named {raw!r} on this computer.",
        candidates=sorted(candidates),
    )
