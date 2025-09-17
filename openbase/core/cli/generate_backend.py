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


async def generate_backend_async(paths: ProjectPaths, config: ProjectConfig):
    """Generate backend code for the project."""
    description_file_path = paths.description_file_path

    # Check if DESCRIPTION.md exists
    if not description_file_path.exists():
        msg = f"DESCRIPTION.md not found at {description_file_path}"
        raise ValueError(msg)

    description = description_file_path.read_text()
    relative_path_to_basic_models_py = paths.basic_models_file_path.relative_to(
        paths.root_dir
    )
    relative_path_to_urls_py = paths.urls_file_path.relative_to(paths.root_dir)

    # Create the prompt for Claude Code
    prompt = dedent_strip_format(
        """
        I am creating the following app:
        {description}
        

        Please complete the backend/API for this app, written in Django and Django REST Framework.  I've already defined a schema for the database in {relative_path_to_basic_models_py} and some endpoints in {relative_path_to_urls_py}.
        
        Please generate the rest of the API code for the app, including serializers.py and views.py.  If necessary, you can also define TaskIQ tasks in the `tasks` module.  I haven't implemented any properties in models.py, or other methods on the models besides __str__, so feel free to do that too, especially if it means keeping code out of views.py.  Please complete the implementation of the API.
        """,
        description=description,
        relative_path_to_basic_models_py=relative_path_to_basic_models_py,
        relative_path_to_urls_py=relative_path_to_urls_py,
    )

    print(prompt)

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


def generate_backend(root_dir: Path):
    """Synchronous wrapper for generate_schema_async."""
    config = ProjectConfig.from_file(get_config_file_path(root_dir))
    paths = ProjectPaths(root_dir, config)
    asyncio.run(generate_backend_async(paths=paths, config=config))


@click.command()
def generate_backend_cli():
    """Generate backend code for the project."""
    current_dir = Path.cwd()
    logger.info(f"Generating backend code for the project in {current_dir}")
    generate_backend(current_dir)
