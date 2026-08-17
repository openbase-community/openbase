"""
Server command for openbase_coder_cli.

This module provides the CLI command to start the Django server.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click

from openbase_coder_cli.cli.node import run_workspace_package_command
from openbase_coder_cli.cli.utils import (
    get_data_dir,
    run_collectstatic,
    run_migrations,
    setup_django_environment,
)
from openbase_coder_cli.plugins.console import sync_console_integration
from openbase_coder_cli.plugins.store import load_registry
from openbase_coder_cli.services.installation import InstallationConfig
from openbase_coder_cli.services.keep_awake import keep_system_awake


@click.command()
@click.option(
    "--host",
    default="127.0.0.1",
    help="Host to bind to.",
    show_default=True,
)
@click.option(
    "--port",
    default=7999,
    type=int,
    help="Port to bind to.",
    show_default=True,
)
@click.option(
    "--workers",
    default=1,
    type=int,
    # Do not pass --workers > 1: websocket group broadcasts silently break
    # across processes (see the gunicorn branch below).
    help="Number of worker processes.",
    show_default=True,
)
@click.option(
    "--reload",
    "reload_",
    is_flag=True,
    help="Enable auto-reload for development.",
)
@click.option(
    "--skip-migrations",
    is_flag=True,
    help="Skip running migrations on startup.",
)
@click.option(
    "--skip-collectstatic",
    is_flag=True,
    help="Skip running collectstatic on startup.",
)
def server(
    host: str,
    port: int,
    workers: int,
    reload_: bool,
    skip_migrations: bool,
    skip_collectstatic: bool,
) -> None:
    """Start the Openbase Coder Cli server."""
    # Set up the Django environment
    setup_django_environment()
    data_dir = get_data_dir()

    click.echo(f"Data directory: {data_dir}")
    click.echo()

    # Run migrations
    if not skip_migrations:
        click.echo("Running migrations...")
        run_migrations()
        click.echo()

    # Run collectstatic
    if not skip_collectstatic:
        click.echo("Collecting static files...")
        run_collectstatic()
        click.echo()

    # Build console
    _build_console()

    # Start the server
    click.echo(f"Starting server at http://{host}:{port}")
    click.echo("Press Ctrl+C to stop.")
    click.echo()

    if workers == 1:
        # Gunicorn always uses a prefork model, even with a single worker. That
        # trips macOS fork-safety crashes in local launchctl mode when Python
        # has already touched Objective-C/CoreFoundation state. Run uvicorn
        # directly for the default local case to avoid forking altogether.
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "openbase_coder_cli.config.asgi:application",
            "--host",
            host,
            "--port",
            str(port),
            "--timeout-keep-alive",
            "0",
            "--log-level",
            "info",
        ]

        if reload_:
            cmd.append("--reload")
    else:
        # WARNING: multi-worker breaks websocket group broadcasts. The channel
        # layer is InMemoryChannelLayer (config/settings.py), which does not
        # span processes: a group_send issued on one worker never reaches a
        # websocket held by another, so thread output streaming and
        # ios_app_control acks fail intermittently depending on which worker
        # handled each connection. Keep --workers at 1 unless the channel
        # layer becomes cross-process (e.g. Redis) first.
        cmd = [
            sys.executable,
            "-m",
            "gunicorn",
            "openbase_coder_cli.config.asgi:application",
            "--bind",
            f"{host}:{port}",
            "--workers",
            str(workers),
            "--worker-class",
            "uvicorn.workers.UvicornWorker",
            "--access-logfile",
            "/dev/null",
            "--error-logfile",
            "-",
            "--timeout",
            "0",  # Disable timeout for long-lived MCP streaming connections
        ]

        if reload_:
            cmd.extend(["--reload"])

    # Run the server
    with keep_system_awake(warn=lambda message: click.echo(message, err=True)):
        try:
            subprocess.run(cmd, env=os.environ, check=True)
        except KeyboardInterrupt:
            click.echo("\nServer stopped.")
        except subprocess.CalledProcessError as e:
            click.echo(f"Server exited with error: {e.returncode}", err=True)
            sys.exit(e.returncode)


def _build_console() -> None:
    if not InstallationConfig.exists():
        click.echo("No installation config found, skipping console build.")
        return

    config = InstallationConfig.load()
    if config.standalone:
        click.echo("Standalone runtime has no writable console source; skipping build.")
        return
    if not config.workspace_path:
        click.echo("No workspace path configured, skipping console build.")
        return

    console_dir = Path(config.workspace_path) / "console"
    if not console_dir.is_dir():
        click.echo(f"Console directory not found at {console_dir}, skipping build.")
        return

    click.echo("Building console...")
    workspace_dir = Path(config.workspace_path)
    if not run_workspace_package_command(workspace_dir, console_dir, "install"):
        return

    # Refresh iframe plugin assets and the runtime console registry
    sync_console_integration(load_registry())

    run_workspace_package_command(workspace_dir, console_dir, "run", "build")
    click.echo("Console build complete.")
    click.echo()
