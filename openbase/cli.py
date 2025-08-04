import os
import secrets
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import click


def get_openbase_directory():
    """Get the directory where the openbase package is installed."""
    # Get the directory where this cli.py file is located
    cli_dir = Path(__file__).parent
    # Return the openbase package directory (where entrypoint/manage.py is)
    return cli_dir


@click.group()
def main():
    """Openbase CLI - AI-powered Django application development."""
    pass


@main.command()
@click.option("--host", default="localhost", help="Host to bind to")
@click.option("--port", default="8001", help="Port to bind to")
@click.option("--no-open", is_flag=True, help="Don't open browser automatically")
def server(host, port, no_open):
    """Start the Openbase development server."""
    openbase_dir = get_openbase_directory()
    manage_py = openbase_dir / "entrypoint" / "manage.py"

    if not manage_py.exists():
        click.echo(f"Error: manage.py not found at {manage_py}")
        sys.exit(1)

    # Set default environment variables for development
    env_defaults = {
        "OPENBASE_SECRET_KEY": secrets.token_hex(64),
        "OPENBASE_PROJECT_DIR": str(Path.cwd()),
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
        f"{host}:{port}",
    ]

    try:
        # Start the server process
        process = subprocess.Popen(cmd, env=env_for_gunicorn)

        # Give the server a moment to start up
        time.sleep(2)

        # Open browser unless --no-open flag is specified
        if not no_open:
            url = f"http://{host}:{port}"
            click.echo(f"Opening browser at {url}")
            webbrowser.open(url)

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


@main.command()
def ttyd():
    """Start ttyd terminal server with Claude integration."""
    click.echo("Starting ttyd terminal server...")

    # Check if ttyd is installed
    try:
        subprocess.run(["which", "ttyd"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        click.echo("Error: ttyd is not installed or not in PATH")
        click.echo("Install ttyd with: brew install ttyd")
        sys.exit(1)

    # Check for zsh and get its path
    try:
        result = subprocess.run(
            ["which", "zsh"], check=True, capture_output=True, text=True
        )
        zsh_path = result.stdout.strip()
    except subprocess.CalledProcessError:
        # Fallback to common zsh locations
        common_zsh_paths = ["/bin/zsh", "/usr/bin/zsh", "/usr/local/bin/zsh"]
        zsh_path = None
        for path in common_zsh_paths:
            if Path(path).exists():
                zsh_path = path
                break

        if not zsh_path:
            click.echo("Error: zsh is not found in PATH or common locations")
            click.echo("Make sure zsh is installed")
            sys.exit(1)

    # Expand home directory for claude path
    home_dir = Path.home()
    claude_path = home_dir / ".claude" / "local" / "claude"

    # Check if claude exists
    if not claude_path.exists():
        click.echo(f"Error: Claude not found at {claude_path}")
        click.echo(
            "Make sure Claude is installed and available at ~/.claude/local/claude"
        )
        sys.exit(1)

    cmd = [
        "ttyd",
        "--interface",
        "127.0.0.1",
        "--writable",
        zsh_path,
        "-c",
        f"{claude_path} --dangerously-skip-permissions; exec {zsh_path}",
    ]

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error running ttyd: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\nTerminal server stopped.")


if __name__ == "__main__":
    main()
