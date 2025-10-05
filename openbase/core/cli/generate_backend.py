"""Generate-backend command for Openbase CLI."""

from __future__ import annotations

import logging
from pathlib import Path

import click

from openbase.core.cli.generation_command import GenerationCommand
from openbase.core.utils import dedent_strip_format

logger = logging.getLogger(__name__)


class GenerateBackendCommand(GenerationCommand):
    """Command to generate backend code for the project."""

    def get_command_description(self) -> str:
        """Return the name of this generation command."""
        return "generate backend"

    async def generate_async(self) -> None:
        """Generate backend code for the project."""
        description_file_path = self.paths.description_file_path

        # Check if DESCRIPTION.md exists
        if not description_file_path.exists():
            msg = f"DESCRIPTION.md not found at {description_file_path}"
            raise ValueError(msg)

        description = description_file_path.read_text()
        relative_path_to_basic_models_py = (
            self.paths.basic_models_file_path.relative_to(self.paths.root_dir)
        )
        relative_path_to_urls_py = self.paths.urls_file_path.relative_to(
            self.paths.root_dir
        )

        # Create the prompt for Claude Code
        prompt = dedent_strip_format(
            """
            I am creating the following app:
            {description}


            Please complete the backend/API for this app, written in Django and Django REST Framework.  I've already defined a schema for the database in {relative_path_to_basic_models_py} and some endpoints in {relative_path_to_urls_py}.

            Please generate the rest of the API code for the app, including serializers.py and views.py.  If necessary, you can also define TaskIQ tasks in the `tasks` module.  I haven't implemented any properties in models.py, or other methods on the models besides __str__, so feel free to do that too, especially if it means keeping code out of views.py.

            Keep in mind:
            - The custom user model has an email but no username
            - Feel free to trigger TaskIQ tasks from the save method of models to trigger tasks when an instance is first created. Alternatively, you can do this from the views.py file.

            Please complete the implementation of the API.
            """,
            description=description,
            relative_path_to_basic_models_py=relative_path_to_basic_models_py,
            relative_path_to_urls_py=relative_path_to_urls_py,
        )

        await self.execute_claude_command(prompt)


def generate_backend(root_dir: Path):
    """Synchronous wrapper for generate_backend_async."""
    command = GenerateBackendCommand(root_dir)
    command.generate()


@click.command()
def generate_backend_cli():
    """Generate backend code for the project."""
    current_dir = Path.cwd()
    logger.info(f"Generating backend code for the project in {current_dir}")
    generate_backend(current_dir)
