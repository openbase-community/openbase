from __future__ import annotations

from pathlib import Path

from multi.app_api import list_project_repo_names


def multi_repo_names(directory: str | Path) -> list[str]:
    """Return sub-repo directory names declared by a workspace multi.json."""
    return list_project_repo_names(directory)


def multi_repo_name_set(directory: str | Path) -> set[str]:
    return set(multi_repo_names(directory))
