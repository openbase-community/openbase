"""Generate-schema command for Openbase CLI."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from openbase.core.claude_code_helper import ClaudeCodeHelper
from openbase.core.paths import ProjectPaths, get_config_file_path
from openbase.core.project_config import ProjectConfig
from openbase.core.utils import dedent_strip_format

logger = logging.getLogger(__name__)


async def generate_frontend_async(paths: ProjectPaths, config: ProjectConfig):
    """Generate frontend code for the project."""
    description = paths.description_file_path.read_text()
    relative_path_to_frontend_dir = paths.frontend_dir.relative_to(paths.root_dir)

    models_py_content = paths.models_file_path.read_text()
    urls_py_content = paths.urls_file_path.read_text()
    api_prefix = config.api_prefix

    # Create the prompt for Claude Code
    prompt = dedent_strip_format(
        """
        I am creating the following app:
        {description}
        
        Please complete the frontend React implementation for this app in {relative_path_to_frontend_dir}. It uses Tailwind for CSS.  Use the following information about the Django backend:
        
        Contents of `models.py`:
        {models_py_content}

        Contents of `urls.py`:
        {urls_py_content}

        Please generate a functional React web app that uses the Django backend. A shell app with login and a dummy dashboard and settings page is already provided for you. When making requests to the backend API, you can use vanilla fetch.  Just make sure you pass header `"X-CSRFToken"` with value of `getCSRFToken()`, which is a function defined in lib/django.ts.  All API requests should be made with the /api/{api_prefix}/ prefix.
        """,
        description=description,
        relative_path_to_frontend_dir=relative_path_to_frontend_dir,
        models_py_content=models_py_content,
        urls_py_content=urls_py_content,
        api_prefix=api_prefix,
    )

    # Initialize Claude Code helper
    claude_helper = ClaudeCodeHelper(project_path=paths.root_dir)

    logger.info("Sending request to Claude Code to generate backend...")
    stdout, stderr, return_code = await claude_helper.execute_claude_command_sync(
        prompt
    )

    if return_code != 0:
        logger.error(f"Claude Code returned non-zero exit code: {return_code}")
        if stderr:
            logger.error(f"Error output: {stderr}")
        msg = "Failed to generate schema. Check the logs for details."
        raise ValueError(msg)

    logger.info("Schema generation completed successfully")

    # Log the output for debugging
    if stdout:
        logger.debug(f"Claude Code output:\n{stdout}")


def generate_frontend(root_dir: Path):
    """Synchronous wrapper for generate_frontend_async."""
    config = ProjectConfig.from_file(get_config_file_path(root_dir))
    paths = ProjectPaths(root_dir, config)
    asyncio.run(generate_frontend_async(paths=paths, config=config))


@click.command()
def generate_frontend_cli():
    """Generate frontend code for the project."""
    current_dir = Path.cwd()
    logger.info(f"Generating frontend code for the project in {current_dir}")
    generate_frontend(current_dir)
