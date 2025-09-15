"""Boilersync management functions for Openbase CLI."""

from __future__ import annotations

import logging
import os
import subprocess
from typing import TYPE_CHECKING

from boilersync.commands.init import init as boilersync_init
from boilersync.names import snake_to_pretty

from openbase.core.git_helpers import get_github_user
from openbase.core.paths import get_boilerplate_dir

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class BoilersyncManager:
    def __init__(
        self,
        root_dir: Path,
        *,
        project_name_snake: str,
        project_name_kebab: str,
        django_app_name: str | None = None,
        marketing_description: str = "Built with Openbase",
    ):
        # Names and variables
        self.project_name_snake = project_name_snake
        self.project_name_kebab = project_name_kebab
        self.api_package_name = f"{project_name_snake}_api"
        self.django_app_name = django_app_name or project_name_snake
        self.marketing_description = marketing_description
        assert all(
            [
                self.project_name_snake,
                self.project_name_kebab,
                self.marketing_description,
                self.api_package_name,
                self.django_app_name,
            ]
        )

        # Paths
        self.root_dir = root_dir
        self.boilerplate_dir = None
        self.api_package_dir = self.root_dir / f"{self.project_name_kebab}-api"
        self.api_package_src_dir = (
            self.api_package_dir / f"{self.project_name_snake}_api"
        )
        self.api_django_app_dir = self.api_package_src_dir / self.django_app_name
        self.api_package_dir.mkdir(parents=True, exist_ok=True)
        self.react_dir = self.root_dir / f"{self.project_name_kebab}-react"

    def clone_or_pull_boilerplate_dir(self):
        """Set up the boilerplate directory, cloning from repo if needed.

        Returns:
            Path: The boilerplate directory path

        Raises:
            subprocess.CalledProcessError: If git clone fails
        """

        # Set up the boilerplate directory
        logger.info("Setting up boilerplate directory...")

        self.boilerplate_dir = get_boilerplate_dir()

        # If boilerplate directory doesn't exist, clone it
        if not self.boilerplate_dir.exists():
            subprocess.run(  # noqa: S603
                [
                    "git",
                    "clone",
                    "https://github.com/openbase-community/openbase-boilerplate.git",
                    str(self.boilerplate_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            # Pull latest changes from origin
            result = subprocess.run(
                ["git", "pull", "origin"],
                cwd=str(self.boilerplate_dir),
                capture_output=True,
                text=True,
                check=False,  # Don't raise exception on non-zero exit
            )
            if result.returncode != 0:
                logger.warning(
                    f"Failed to pull latest changes from origin: {result.stderr}"
                )

        logger.info(f"Using boilerplate directory: {self.boilerplate_dir}")

    def init_boilersync_api_package(
        self,
    ):
        """Initialize boilersync app-package template."""
        os.environ["BOILERSYNC_TEMPLATE_DIR"] = str(self.boilerplate_dir)

        apps = f'"{self.api_package_name}.{self.django_app_name}"'

        boilersync_init(
            template_name="app-package",
            target_dir=self.api_package_dir,
            no_input=True,
            collected_variables={
                "apps": apps,
                "name_snake": self.api_package_name,
            },
        )

    def init_boilersync_django_app(self):
        """Initialize boilersync django-app template."""
        self.api_django_app_dir.mkdir(parents=True, exist_ok=True)

        boilersync_init(
            template_name="django-app",
            target_dir=self.api_django_app_dir,
            collected_variables={
                "name_snake": self.django_app_name,
                "parent_package_name": self.api_package_name,
            },
            no_input=True,
        )

    def init_boilersync_react_app(self):
        """Initialize boilersync react-app template."""
        self.react_dir.mkdir(parents=True, exist_ok=True)

        boilersync_init(
            template_name="react-app",
            target_dir=self.react_dir,
            no_input=True,
            collected_variables={
                "name_snake": self.project_name_snake,
                "name_pretty": snake_to_pretty(self.project_name_snake),
                "github_user": get_github_user(),
                "marketing_description": self.marketing_description,
            },
        )

    def update_and_init_all(self):
        """Update and initialize all templates."""
        self.clone_or_pull_boilerplate_dir()
        self.init_boilersync_api_package()
        self.init_boilersync_django_app()
        self.init_boilersync_react_app()
