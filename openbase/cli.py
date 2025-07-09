import os
import secrets
import subprocess
import sys
from pathlib import Path

import click


def get_openbase_directory():
    """Get the directory where the openbase package is installed."""
    # Get the directory where this cli.py file is located
    cli_dir = Path(__file__).parent
    # Go up one level to get the openbase project directory (where manage.py is)
    return cli_dir.parent


@click.group()
def main():
    """Openbase CLI - AI-powered Django application development."""
    pass


@main.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default="8001", help="Port to bind to")
def server(host, port):
    """Start the Openbase development server."""
    openbase_dir = get_openbase_directory()
    manage_py = openbase_dir / "manage.py"

    if not manage_py.exists():
        click.echo(f"Error: manage.py not found at {manage_py}")
        sys.exit(1)

    # Set default environment variables for development
    env_defaults = {
        "SECRET_KEY": secrets.token_hex(64),
        "DJANGO_PROJECT_DIR": str(Path.cwd()),
        "DJANGO_PROJECT_APPS_DIR": str(Path.cwd()),
    }

    # Only set defaults if not already set
    for key, value in env_defaults.items():
        if not os.environ.get(key):
            os.environ[key] = value

    # Change to the openbase directory
    os.chdir(openbase_dir)

    # Run migrations first
    click.echo("Running migrations...")
    migrate_cmd = [sys.executable, str(manage_py), "migrate"]
    try:
        subprocess.run(migrate_cmd, check=True)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error running migrations: {e}")
        sys.exit(1)

    # Start the server with gunicorn
    click.echo(f"Starting server on {host}:{port}")

    # Set environment variables for gunicorn
    env = os.environ.copy()
    env["HOST"] = host
    env["PORT"] = port

    cmd = [
        "gunicorn",
        "openbase.config.asgi:application",
        "--log-file",
        "-",
        "-k",
        "uvicorn.workers.UvicornWorker",
        "--bind",
        f"{host}:{port}",
    ]

    try:
        subprocess.run(cmd, check=True, env=env)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error running server: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\nServer stopped.")


if __name__ == "__main__":
    main()
