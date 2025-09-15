"""Git helper functions for Openbase CLI."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def create_github_repo(project_name_kebab: str):
    """Create a GitHub repository if it doesn't exist.

    Returns:
        Status message about the repository creation.

    Raises:
        subprocess.CalledProcessError: If repo creation fails
        Exception: If repo check/creation encounters other errors
    """

    github_user = get_github_user()

    # Check if repo exists
    check_result = subprocess.run(
        ["gh", "repo", "view", f"{github_user}/{project_name_kebab}"],
        capture_output=True,
        text=True,
        check=False,
    )

    if check_result.returncode != 0:
        # Repo doesn't exist, create it
        subprocess.run(
            [
                "gh",
                "repo",
                "create",
                f"{github_user}/{project_name_kebab}",
                "--private",
            ],
            capture_output=True,
            text=True,
            check=True,
        )


def init_git_repo(current_dir: Path) -> None:
    """Initialize a git repository in the given directory.

    Raises:
        subprocess.CalledProcessError: If git init fails
    """
    subprocess.run(
        ["git", "init"],
        cwd=str(current_dir),
        capture_output=True,
        text=True,
        check=True,
    )


def create_initial_commit(current_dir: Path) -> None:
    """Create an initial git commit.

    Raises:
        subprocess.CalledProcessError: If git add or commit fails
    """
    subprocess.run(
        ["git", "add", "."],
        cwd=str(current_dir),
        capture_output=True,
        text=True,
        check=True,
    )

    subprocess.run(
        ["git", "commit", "-am", "Initial commit"],
        cwd=str(current_dir),
        capture_output=True,
        text=True,
        check=True,
    )


def get_github_user() -> str:
    """Get the current GitHub user's username.

    Returns:
        The GitHub username of the authenticated user.

    Raises:
        subprocess.CalledProcessError: If gh command fails
    """
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()
