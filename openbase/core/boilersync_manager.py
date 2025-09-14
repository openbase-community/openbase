"""Boilersync management functions for Openbase CLI."""

import os
import subprocess
from pathlib import Path

from boilersync.commands.init import init as boilersync_init

from openbase.core.paths import get_boilerplate_dir, get_openbase_dir


def setup_boilerplate_dir():
    """Set up the boilerplate directory, cloning from repo if needed.

    Returns:
        Path: The boilerplate directory path

    Raises:
        subprocess.CalledProcessError: If git clone fails
    """
    openbase_dir = get_openbase_dir()
    boilerplate_dir = get_boilerplate_dir()

    # Create ~/.openbase if it doesn't exist
    openbase_dir.mkdir(parents=True, exist_ok=True)

    # If boilerplate directory doesn't exist, clone it
    if not boilerplate_dir.exists():
        subprocess.run(
            [
                "git",
                "clone",
                "https://github.com/openbase-community/openbase-boilerplate.git",
                str(boilerplate_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        # Pull latest changes from origin
        result = subprocess.run(
            ["git", "pull", "origin"],
            cwd=str(boilerplate_dir),
            capture_output=True,
            text=True,
            check=False,  # Don't raise exception on non-zero exit
        )
        if result.returncode != 0:
            # Non-fatal, just continue with existing boilerplate
            pass

    return boilerplate_dir


def init_boilersync_app_package(
    boilerplate_dir: Path,
    current_dir: Path,
    project_name_kebab: str,
    project_name_snake: str,
    app_name: str,
):
    """Initialize boilersync app-package template."""
    # Set the BOILERSYNC_TEMPLATE_DIR environment variable
    os.environ["BOILERSYNC_TEMPLATE_DIR"] = str(boilerplate_dir)

    package_name_snake = project_name_snake
    apps = f'"{package_name_snake}.{app_name}"'
    app_package_dir = current_dir / project_name_kebab
    app_package_dir.mkdir(parents=True, exist_ok=True)

    boilersync_init(
        "app-package",
        app_package_dir,
        collected_variables={
            "apps": apps,
            "name_snake": project_name_snake,
            "name_kebab": project_name_kebab,
        },
    )

    return app_package_dir, apps


def init_boilersync_django_app(
    app_package_dir: Path, project_name_snake: str, app_name: str, apps: str
):
    """Initialize boilersync django-app template."""
    app_dir = app_package_dir / project_name_snake / app_name
    app_dir.mkdir(parents=True, exist_ok=True)

    boilersync_init(
        "django-app",
        app_dir,
        collected_variables={"apps": apps},
    )

    return app_dir


def init_boilersync_react_app(
    app_package_dir: Path, project_name_kebab: str, project_name_snake: str
):
    """Initialize boilersync react-app template."""
    react_dir = app_package_dir / project_name_kebab / "react"
    react_dir.mkdir(parents=True, exist_ok=True)

    boilersync_init("react-app", react_dir)
