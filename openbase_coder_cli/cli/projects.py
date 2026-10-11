"""Create project folders and register them in Openbase's project list."""

from __future__ import annotations

import json
from pathlib import Path

import click

from openbase_coder_cli.thread_sync.projects import (
    _is_ignored_project_path,
    get_recent_projects,
    project_root_for_thread_directory,
    track_project,
)


@click.group()
def projects() -> None:
    """Create, register and list projects on this computer."""


def _project_root(path: Path) -> str:
    root = project_root_for_thread_directory(str(path))
    if _is_ignored_project_path(root):
        raise click.ClickException(
            "Choose a dedicated project folder outside system directories and the home root."
        )
    return root


@projects.command("create")
@click.argument("path", type=click.Path(path_type=Path))
def create(path: Path) -> None:
    """Create PATH and register it. Print its absolute path for an agent's cwd.

    Existing directories are registered without changing their contents.
    This command does not scaffold code, initialize Git or publish a repository.
    """
    path = path.expanduser().resolve()
    try:
        root = _project_root(path)
        path.mkdir(parents=True, exist_ok=True)
        track_project(root)
    except OSError as exc:
        raise click.ClickException(
            f"Unable to create or register project: {exc}"
        ) from exc
    click.echo(root)


@projects.command("add")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
def add(path: Path) -> None:
    """Register an existing project folder without changing its contents."""
    try:
        root = _project_root(path.expanduser().resolve())
        track_project(root)
    except OSError as exc:
        raise click.ClickException(f"Unable to register project: {exc}") from exc
    click.echo(root)


@projects.command("list")
def list_projects() -> None:
    """Print registered and automatically discovered projects as JSON."""
    click.echo(json.dumps(get_recent_projects(), indent=2))
