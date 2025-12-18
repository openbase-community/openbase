"""Generate command for Openbase CLI - runs schema, backend, and frontend generation."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from openbase.core.cli.generate_backend import GenerateBackendCommand
from openbase.core.cli.generate_frontend import GenerateFrontendCommand
from openbase.core.cli.generate_schema import GenerateSchemaCommand

logger = logging.getLogger(__name__)


async def generate_async(root_dir: Path, parallel: bool) -> None:
    """Run schema generation, then backend and frontend generation.

    Args:
        root_dir: The project root directory.
        parallel: If True, run backend and frontend generation in parallel.
                  If False, run them sequentially.
    """
    # First, run schema generation
    logger.info("Starting schema generation...")
    schema_command = GenerateSchemaCommand(root_dir)
    await schema_command.generate_async()
    logger.info("Schema generation completed.")

    # Then run backend and frontend generation
    backend_command = GenerateBackendCommand(root_dir)
    frontend_command = GenerateFrontendCommand(root_dir)

    if parallel:
        logger.info("Starting backend and frontend generation in parallel...")
        await asyncio.gather(
            backend_command.generate_async(),
            frontend_command.generate_async(),
        )
    else:
        logger.info("Starting backend generation...")
        await backend_command.generate_async()
        logger.info("Backend generation completed. Starting frontend generation...")
        await frontend_command.generate_async()

    logger.info("All generation tasks completed.")


def generate(root_dir: Path, parallel: bool) -> None:
    """Synchronous wrapper for generate_async."""
    asyncio.run(generate_async(root_dir, parallel))


@click.command()
@click.option(
    "--parallel/--sequential",
    default=True,
    help="Run backend and frontend generation in parallel (default) or sequentially.",
)
def generate_cli(parallel: bool):
    """Generate schema, backend, and frontend code for the project.

    This command runs the full generation pipeline:
    1. First, generates Django schema (models.py and urls.py) from DESCRIPTION.md
    2. Then, generates backend and frontend code either in parallel or sequentially

    Use --parallel (default) to run backend and frontend generation concurrently.
    Use --sequential to run them one after the other.
    """
    current_dir = Path.cwd()
    logger.info(f"Starting full generation for project in {current_dir}")
    generate(current_dir, parallel)
