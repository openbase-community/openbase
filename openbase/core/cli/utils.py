"""Shared utilities for Openbase CLI commands."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import webbrowser
from pathlib import Path

import click


def setup_environment():
    """Set up environment variables and run migrations."""
    # Get the openbase entrypoint directory
    entrypoint_dir = Path(__file__).parent.parent.parent
    manage_py = entrypoint_dir / "entrypoint" / "manage.py"

    if not manage_py.exists():
        click.echo(f"Error: manage.py not found at {manage_py}")
        sys.exit(1)

    # Set default environment variables for development
    # TODO: Combine with server stuff
    env_defaults = {
        "OPENBASE_SECRET_KEY": secrets.token_hex(64),
        "OPENBASE_PROJECT_DIR": str(Path.cwd()),
        "OPENBASE_API_TOKEN": secrets.token_hex(32),
    }

    # Only set defaults if not already set
    for key, value in env_defaults.items():
        if not os.environ.get(key):
            os.environ[key] = value

    print("OPENBASE_API_TOKEN", os.environ["OPENBASE_API_TOKEN"])

    # Save current directory to restore later
    original_cwd = Path.cwd()

    # Change to the entrypoint directory for running migrations
    os.chdir(entrypoint_dir)

    # Run migrations first
    click.echo("Running migrations...")
    migrate_cmd = [sys.executable, str(manage_py), "migrate"]
    try:
        subprocess.run(migrate_cmd, check=True)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error running migrations: {e}")
        sys.exit(1)

    # Run collectstatic
    click.echo("Running collectstatic...")
    collectstatic_cmd = [sys.executable, str(manage_py), "collectstatic", "--noinput"]
    try:
        subprocess.run(collectstatic_cmd, check=True)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error running collectstatic: {e}")
        sys.exit(1)

    # Restore original working directory
    os.chdir(original_cwd)

    return manage_py


def open_browser(host, port):
    """Open browser at the given host:port."""
    url = f"http://{host}:{port}"
    click.echo(f"Opening browser at {url}")
    webbrowser.open(url)
