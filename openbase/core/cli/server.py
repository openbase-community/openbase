"""Server command for Openbase CLI."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import click

from .utils import setup_environment


def start_server_process(host, port):
    """Start the gunicorn server process."""
    click.echo(f"Starting server on {host}:{port}")

    # Set environment variables for gunicorn
    env_for_gunicorn = os.environ.copy()
    env_for_gunicorn["OPENBASE_ALLOWED_HOSTS"] = host

    cmd = [
        sys.executable,
        "-m",
        "gunicorn",
        "openbase.config.asgi:application",
        "--log-file",
        "-",
        "-k",
        "uvicorn.workers.UvicornWorker",
        "--bind",
        f"0.0.0.0:{port}",
    ]

    return subprocess.Popen(cmd, env=env_for_gunicorn)


@click.command()
@click.option(
    "--host",
    default="localhost",
    help="Host to bind to",
)
@click.option("--port", default="8001", help="Port to bind to")
@click.option("--no-open", is_flag=True, help="Don't open browser automatically")
def server(host, port, no_open):
    """Start the Openbase development server."""
    setup_environment()

    try:
        # Start the server process
        process = start_server_process(host, port)

        # Give the server a moment to start up
        time.sleep(2)

        # Open browser unless --no-open flag is specified
        if not no_open:
            # open_browser(host, port)
            pass

        # Wait for the process to complete
        process.wait()
    except subprocess.CalledProcessError as e:
        click.echo(f"Error running server: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\nServer stopped.")
        if process.poll() is None:  # Process is still running
            process.terminate()
            process.wait()
