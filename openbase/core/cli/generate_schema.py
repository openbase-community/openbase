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


async def generate_schema_async(paths: ProjectPaths, config: ProjectConfig):
    """Generate Django schema (models.py and urls.py) based on DESCRIPTION.md."""
    description_file_path = paths.description_file_path

    # Check if DESCRIPTION.md exists
    if not description_file_path.exists():
        msg = f"DESCRIPTION.md not found at {description_file_path}"
        raise ValueError(msg)

    relative_path_to_description_md = description_file_path.relative_to(paths.root_dir)
    relative_path_to_models_py = paths.models_file_path.relative_to(paths.root_dir)
    relative_path_to_urls_py = paths.urls_file_path.relative_to(paths.root_dir)

    # Create the prompt for Claude Code
    prompt = dedent_strip_format(
        """
        Based on the app description found in {relative_path_to_description_md}, please generate Django models.py and urls.py files ({relative_path_to_models_py} and {relative_path_to_urls_py} respectively).  Right now these files are more or less empty.

        IMPORTANT:
        - ONLY modify the models.py and urls.py files in the Django app
        - Do NOT include any other methods on the models besides __str__.  The only exception to this is if invariants need to be maintained on a model's fields, in which case you can add clean and save methods that are limited to performing the necessary validation and/or providing of default values.
        - Do NOT implement any views or serializers - just implement bare-bones models.py and urls.py files.  We will do the rest later after I check the schema.
        - Do NOT modify any other files.

        Please generate {relative_path_to_models_py} and {relative_path_to_urls_py} now.
        """,
        relative_path_to_description_md=relative_path_to_description_md,
        relative_path_to_models_py=relative_path_to_models_py,
        relative_path_to_urls_py=relative_path_to_urls_py,
    )

    # Initialize Claude Code helper
    claude_helper = ClaudeCodeHelper(project_path=paths.root_dir)

    logger.info("Sending request to Claude Code to generate schema...")
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

    # Save the output to the models and urls files
    basic_models_content = paths.models_file_path.read_text()
    paths.basic_models_file_path.write_text(basic_models_content)


def generate_schema(root_dir: Path):
    """Synchronous wrapper for generate_schema_async."""
    config = ProjectConfig.from_file(get_config_file_path(root_dir))
    paths = ProjectPaths(root_dir, config)
    asyncio.run(generate_schema_async(paths=paths, config=config))


@click.command()
def generate_schema_cli():
    """Generate Django schema (models.py and urls.py) from DESCRIPTION.md.

    This command reads the DESCRIPTION.md file from the current directory
    and uses Claude Code to generate appropriate Django models and URL patterns.
    """
    current_dir = Path.cwd()
    logger.info(f"Generating schema for Django app in {current_dir}")
    generate_schema(current_dir)
