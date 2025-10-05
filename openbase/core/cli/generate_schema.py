"""Generate-schema command for Openbase CLI."""

from __future__ import annotations

import logging
from pathlib import Path

import click

from openbase.core.cli.generation_command import GenerationCommand
from openbase.core.utils import dedent_strip_format

logger = logging.getLogger(__name__)


class GenerateSchemaCommand(GenerationCommand):
    """Command to generate Django schema from DESCRIPTION.md."""

    def get_command_description(self) -> str:
        """Return the name of this generation command."""
        return "generate schema"

    async def generate_async(self) -> None:
        """Generate Django schema (models.py and urls.py) based on DESCRIPTION.md."""
        description_file_path = self.paths.description_file_path

        # Check if DESCRIPTION.md exists
        if not description_file_path.exists():
            msg = f"DESCRIPTION.md not found at {description_file_path}"
            raise ValueError(msg)

        relative_path_to_description_md = description_file_path.relative_to(
            self.paths.root_dir
        )
        relative_path_to_models_py = self.paths.models_file_path.relative_to(
            self.paths.root_dir
        )
        relative_path_to_urls_py = self.paths.urls_file_path.relative_to(
            self.paths.root_dir
        )

        # Create the prompt for Claude Code
        prompt = dedent_strip_format(
            """
            Based on the app description found in {relative_path_to_description_md}, please generate Django models.py and urls.py files ({relative_path_to_models_py} and {relative_path_to_urls_py} respectively).  Right now these files are more or less empty.

            IMPORTANT:
            - ONLY modify the models.py and urls.py files in the Django app
            - Do NOT include any other methods on the models besides __str__.  The only exception to this is if invariants need to be maintained on a model's fields, in which case you can add clean and save methods that are limited to performing the necessary validation and/or providing of default values.
            - Do NOT implement any views or serializers - just implement bare-bones models.py and urls.py files.  We will do the rest later after I check the schema.
            - Do NOT modify any other files.
            - Keep in mind when implementing __str__ that a user has an email but no username (but better yet, you can just use {{user}} in your formatting to defer to the user object's __str__ method)

            Please generate {relative_path_to_models_py} and {relative_path_to_urls_py} now.
            """,
            relative_path_to_description_md=relative_path_to_description_md,
            relative_path_to_models_py=relative_path_to_models_py,
            relative_path_to_urls_py=relative_path_to_urls_py,
        )

        await self.execute_claude_command(prompt)

        # Save the output to the basic_models file
        basic_models_content = self.paths.models_file_path.read_text()
        self.paths.basic_models_file_path.write_text(basic_models_content)


def generate_schema(root_dir: Path):
    """Synchronous wrapper for generate_schema_async."""
    command = GenerateSchemaCommand(root_dir)
    command.generate()


@click.command()
def generate_schema_cli():
    """Generate Django schema (models.py and urls.py) from DESCRIPTION.md.

    This command reads the DESCRIPTION.md file from the current directory
    and uses Claude Code to generate appropriate Django models and URL patterns.
    """
    current_dir = Path.cwd()
    logger.info(f"Generating schema for Django app in {current_dir}")
    generate_schema(current_dir)
