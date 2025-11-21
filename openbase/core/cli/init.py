"""Init command for Openbase CLI."""

from __future__ import annotations

import logging
from pathlib import Path

import click
from boilersync.names import normalize_to_snake

from openbase.core.paths import ProjectPaths, get_config_file_path
from openbase.core.project_config import ProjectConfig
from openbase.core.project_scaffolder import ProjectScaffolder

logger = logging.getLogger(__name__)


def init(
    root_dir,
    *,
    with_frontend: bool = True,
    with_github: bool = False,
):
    """Initialize a new Openbase project in the specified directory.

    Args:
        project_dir: The directory where the project should be initialized
        with_frontend: Whether to initialize a frontend (React) app
        with_github: Whether to create a GitHub repository
    """

    config_file_path = get_config_file_path(root_dir)
    if config_file_path.exists():
        config = ProjectConfig.from_file(config_file_path)
    else:
        project_name_kebab = root_dir.name
        project_name_snake = normalize_to_snake(project_name_kebab)
        api_package_name = f"{project_name_snake}_api"
        api_prefix = "api/" + project_name_snake

        config = ProjectConfig(
            project_name_snake=project_name_snake,
            project_name_kebab=project_name_kebab,
            api_package_name=api_package_name,
            django_app_name=project_name_snake,
            marketing_description="Built with Openbase",
            api_prefix=api_prefix,
        )
        config.to_file(config_file_path)

    paths = ProjectPaths(root_dir, config)
    project_scaffolder = ProjectScaffolder(
        paths=paths,
        config=config,
        with_frontend=with_frontend,
        with_github=with_github,
    )
    project_scaffolder.init_with_boilersync_and_git()


@click.command()
@click.option(
    "--with-frontend",
    default=True,
    help="Initialize a frontend (React app) as well.",
)
@click.option(
    "--with-github",
    default=False,
    help="Initialize a GitHub repository as well.",
)
def init_cli(with_frontend, with_github):
    """Initialize a new Openbase project in the current directory.

    By default, this will also initialize a frontend (React) app. Use --no-frontend to skip frontend initialization.
    """
    current_dir = Path.cwd()
    init(current_dir, with_frontend=with_frontend, with_github=with_github)
