"""Main CLI entry point for Openbase."""

import click

from .default import default
from .init import init
from .server import server
from .ttyd import ttyd
from .watcher import watcher


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx):
    """Openbase CLI - AI-powered Django application development."""
    # If no command is provided, run the default command
    if ctx.invoked_subcommand is None:
        # Call the default command which runs both server and ttyd
        ctx.invoke(default)


# Register all commands
main.add_command(init)
main.add_command(server)
main.add_command(ttyd)
main.add_command(watcher)
main.add_command(default)


if __name__ == "__main__":
    main()