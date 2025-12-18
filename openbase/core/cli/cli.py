"""Main CLI entry point for Openbase."""

from __future__ import annotations

import logging
import os

import click

from .default import default
from .generate import generate_cli
from .generate_schema import generate_schema_cli
from .init import init_cli
from .server import server
from .watcher import watcher


def setup_logging():
    """Configure logging for CLI commands."""
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(levelname)s %(asctime)s %(module)s %(message)s",
    )


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx):
    """Openbase CLI - AI-powered Django application development."""
    setup_logging()
    # If no command is provided, run the default command
    if ctx.invoked_subcommand is None:
        # Call the default command which runs both server and ttyd
        ctx.invoke(default)


# Register all commands
main.add_command(init_cli, name="init")
main.add_command(generate_cli, name="generate")
main.add_command(generate_schema_cli, name="generate-schema")
main.add_command(server)
main.add_command(watcher)
main.add_command(default)


if __name__ == "__main__":
    main()
