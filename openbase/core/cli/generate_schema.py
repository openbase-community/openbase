"""Generate-schema command for Openbase CLI."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from openbase.core.claude_code_helper import ClaudeCodeHelper
from openbase.core.utils import dedent_strip

logger = logging.getLogger(__name__)


async def generate_schema_async(root_dir: Path):
    """Generate Django schema (models.py and urls.py) based on DESCRIPTION.md.

    Args:
        root_dir: The root directory of the Django app
    """
    description_file = root_dir / "DESCRIPTION.md"

    # Check if DESCRIPTION.md exists
    if not description_file.exists():
        msg = f"DESCRIPTION.md not found at {description_file}"
        raise ValueError(msg)

    # Create the prompt for Claude Code
    prompt = dedent_strip(
        """
        Based on the app description found in DESCRIPTION.md, please generate Django models.py and urls.py files.

        IMPORTANT:
        - ONLY modify the models.py and urls.py files in the Django app
        - Do NOT include any other methods on the models besides __str__.  The only exception to this is if invariants need to be maintained on the model's fields, in which case you can add clean and save methods that are limited to performing necessary validation and/or providing of default values.
        - Do NOT implement any views or serializers - just bare-bones models and urls.py files.
        - Do NOT modify any other files.

        Please generate models.py and urls.py now.
        """
    )

    # Initialize Claude Code helper
    claude_helper = ClaudeCodeHelper(project_path=root_dir)

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


def generate_schema(root_dir: Path):
    """Synchronous wrapper for generate_schema_async."""
    asyncio.run(generate_schema_async(root_dir))


@click.command()
def generate_schema_cli():
    """Generate Django schema (models.py and urls.py) from DESCRIPTION.md.

    This command reads the DESCRIPTION.md file from the current directory
    and uses Claude Code to generate appropriate Django models and URL patterns.
    """
    current_dir = Path.cwd()
    logger.info(f"Generating schema for Django app in {current_dir}")
    generate_schema(current_dir)
