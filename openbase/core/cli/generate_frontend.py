"""Generate-frontend command for Openbase CLI."""

from __future__ import annotations

import logging
from pathlib import Path

import click

from openbase.core.cli.generation_command import GenerationCommand
from openbase.core.utils import dedent_strip_format

logger = logging.getLogger(__name__)


class GenerateFrontendCommand(GenerationCommand):
    """Command to generate frontend code for the project."""

    def get_command_description(self) -> str:
        """Return the name of this generation command."""
        return "generate frontend"

    async def generate_async(self) -> None:
        """Generate frontend code for the project."""
        description = self.paths.description_file_path.read_text()
        relative_path_to_frontend_dir = self.paths.react_dir.relative_to(
            self.paths.root_dir
        )

        models_py_content = self.paths.models_file_path.read_text()
        urls_py_content = self.paths.urls_file_path.read_text()
        api_prefix = self.config.api_prefix

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

        await self.execute_claude_command(prompt)


def generate_frontend(root_dir: Path):
    """Synchronous wrapper for generate_frontend_async."""
    command = GenerateFrontendCommand(root_dir)
    command.generate()


@click.command()
def generate_frontend_cli():
    """Generate frontend code for the project."""
    current_dir = Path.cwd()
    logger.info(f"Generating frontend code for the project in {current_dir}")
    generate_frontend(current_dir)
