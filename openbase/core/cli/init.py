"""Init command for Openbase CLI."""

from __future__ import annotations

import inspect
import logging
import os
import secrets
from pathlib import Path

import click
from boilersync.commands.init import init as boilersync_init
from boilersync.names import normalize_to_snake, snake_to_pretty

from openbase.core.default_env import make_default_env
from openbase.core.git_helpers import get_github_user
from openbase.core.paths import ProjectPaths, get_config_file_path
from openbase.core.project_config import ProjectConfig
from openbase.core.project_scaffolder import ProjectScaffolder
from openbase.core.template_manager import TemplateManager

logger = logging.getLogger(__name__)
WORKSPACE_TEMPLATE_NAME = "django-react-workspace"


def _boilersync_supports_workspace_init() -> bool:
    signature = inspect.signature(boilersync_init)
    return "template_variables" in signature.parameters and "options" in signature.parameters


def _build_workspace_template_variables(
    config: ProjectConfig,
    *,
    with_frontend: bool,
    with_github: bool,
) -> dict[str, str | bool]:
    dot_env_symlink_source = os.getenv("DOT_ENV_SYMLINK_SOURCE", "")
    openbase_web_env_content = ""
    if not dot_env_symlink_source:
        openbase_web_env_content = make_default_env(
            package_name_snake=config.project_name_snake,
            package_name_url_prefix=config.api_prefix,
            openbase_secret_key=secrets.token_hex(32),
            openbase_api_token=secrets.token_hex(32),
            django_secret_key=secrets.token_hex(32),
        )

    return {
        "api_package_name": config.api_package_name,
        "api_package_name_snake": config.api_package_name_snake,
        "django_app_name": config.django_app_name,
        "api_prefix": config.api_prefix,
        "github_user": get_github_user(),
        "marketing_description": config.marketing_description,
        "dot_env_symlink_source": dot_env_symlink_source,
        "openbase_web_env_content": openbase_web_env_content,
        "with_frontend": with_frontend,
        "with_github": with_github,
    }


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
    template_manager = TemplateManager(paths=paths, config=config)
    template_manager.clone_or_pull_boilerplate_dir()

    if _boilersync_supports_workspace_init():
        os.environ["BOILERSYNC_TEMPLATE_DIR"] = str(template_manager.boilerplate_dir)
        template_variables = _build_workspace_template_variables(
            config,
            with_frontend=with_frontend,
            with_github=with_github,
        )
        try:
            boilersync_init(
                template_name=WORKSPACE_TEMPLATE_NAME,
                target_dir=root_dir,
                project_name=config.project_name_snake,
                pretty_name=snake_to_pretty(config.project_name_snake),
                template_variables=template_variables,
                options={
                    "with_frontend": with_frontend,
                    "with_github": with_github,
                },
                no_input=True,
            )
            return
        except (TypeError, FileNotFoundError) as exc:
            logger.warning(
                "Falling back to legacy Openbase scaffolder because workspace template init failed: %s",
                exc,
            )

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
