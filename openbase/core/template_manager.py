"""Template management functions for Openbase CLI."""

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
    from openbase.core.paths import ProjectPaths
    from openbase.core.project_config import ProjectConfig

logger = logging.getLogger(__name__)


class TemplateManager:
    def __init__(
        self,
        paths: ProjectPaths,
        config: ProjectConfig,
    ):
        self.paths = paths
        self.config = config
        self.boilerplate_dir = get_boilerplate_dir()

    def clone_or_pull_boilerplate_dir(self):
        """Set up the boilerplate directory, cloning from repo if needed.

        Returns:
            Path: The boilerplate directory path

        Raises:
            subprocess.CalledProcessError: If git clone fails
        """

        # Set up the boilerplate directory
        logger.info("Setting up boilerplate directory...")

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

        apps = f'"{self.config.api_package_name}.{self.config.django_app_name}"'

        api_package_dir = self.paths.api_package_dir
        api_package_dir.mkdir(parents=True, exist_ok=True)
        boilersync_init(
            template_name="app-package",
            target_dir=api_package_dir,
            no_input=True,
            collected_variables={
                "apps": apps,
                "name_snake": self.config.api_package_name,
            },
        )

    def init_boilersync_django_app(self):
        """Initialize boilersync django-app template."""
        self.paths.api_django_app_dir.mkdir(parents=True, exist_ok=True)

        boilersync_init(
            template_name="django-app",
            target_dir=self.paths.api_django_app_dir,
            collected_variables={
                "name_snake": self.config.django_app_name,
                "parent_package_name": self.config.api_package_name,
                "api_prefix": self.config.api_prefix,
            },
            no_input=True,
        )

    def init_boilersync_react_app(self):
        """Initialize boilersync react-app template."""
        self.paths.react_dir.mkdir(parents=True, exist_ok=True)

        boilersync_init(
            template_name="react-app",
            target_dir=self.paths.react_dir,
            no_input=True,
            collected_variables={
                "name_snake": self.config.project_name_snake,
                "name_pretty": snake_to_pretty(self.config.project_name_snake),
                "github_user": get_github_user(),
                "marketing_description": self.config.marketing_description,
            },
        )

    def update_and_init_all(self):
        """Update and initialize all templates."""
        self.clone_or_pull_boilerplate_dir()
        self.init_boilersync_api_package()
        self.init_boilersync_django_app()
        self.init_boilersync_react_app()
