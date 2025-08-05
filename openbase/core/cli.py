import os
import secrets
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import click

from .directory_watcher import DirectoryWatcher


def setup_environment():
    """Set up environment variables and run migrations."""
    # Get the openbase entrypoint directory
    entrypoint_dir = Path(__file__).parent
    manage_py = entrypoint_dir / "entrypoint" / "manage.py"

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

    # Restore original working directory
    os.chdir(original_cwd)

    return manage_py


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
        f"{host}:{port}",
    ]

    return subprocess.Popen(cmd, env=env_for_gunicorn)


def check_and_get_ttyd_setup():
    """Check ttyd installation and get zsh/claude paths."""
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

    return zsh_path, claude_path


def build_ttyd_command(zsh_path, claude_path, include_theme=False):
    """Build the ttyd command array."""
    cmd = ["ttyd"]

    if include_theme:
        cmd.extend(["-t", '\'theme={"background": "green"}\''])

    cmd.extend(
        [
            "-t",
            'theme={"background": "white", "foreground": "black"}',
            "-t",
            'fontFamily="Menlo","Consolas"',
            "--interface",
            "127.0.0.1",
            "--writable",
            zsh_path,
            "-c",
            f"cd {Path.cwd()}; {claude_path} --dangerously-skip-permissions; exec {zsh_path}",
        ]
    )

    return cmd


def start_ttyd_process(zsh_path, claude_path):
    """Start the ttyd process from the current working directory."""
    cmd = build_ttyd_command(zsh_path, claude_path, include_theme=True)
    print(cmd)
    return subprocess.Popen(cmd)


def open_browser_if_requested(host, port, no_open):
    """Open browser at the given host:port unless no_open is True."""
    if not no_open:
        url = f"http://{host}:{port}"
        click.echo(f"Opening browser at {url}")
        webbrowser.open(url)


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx):
    """Openbase CLI - AI-powered Django application development."""
    # If no command is provided, run the default command
    if ctx.invoked_subcommand is None:
        # Call the default command which runs both server and ttyd
        ctx.invoke(default)


@main.command()
@click.option("--host", default="localhost", help="Host to bind to")
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
        open_browser_if_requested(host, port, no_open)

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

    zsh_path, claude_path = check_and_get_ttyd_setup()

    try:
        cmd = build_ttyd_command(zsh_path, claude_path, include_theme=False)
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error running ttyd: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\nTerminal server stopped.")


@main.command()
@click.option("--host", default="localhost", help="Host to bind to")
@click.option("--port", default="8001", help="Port to bind to")
def watcher(host, port):
    """Run only the directory watcher."""
    click.echo("Starting directory watcher...")

    # Create watcher with server URL
    server_url = f"http://{host}:{port}"
    watcher = DirectoryWatcher(server_url=server_url)
    watcher.start()

    try:
        # Keep the watcher running
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        click.echo("\nStopping watcher...")
        watcher.stop()
        click.echo("Watcher stopped.")


@main.command()
@click.option("--host", default="localhost", help="Host to bind to")
@click.option("--port", default="8001", help="Port to bind to")
@click.option("--no-open", is_flag=True, help="Don't open browser automatically")
def default(host, port, no_open):
    """Default command that runs both server and ttyd with directory watcher."""
    click.echo("Starting Openbase with server, ttyd, and directory watcher...")

    # Start the directory watcher
    server_url = f"http://{host}:{port}"
    watcher = DirectoryWatcher(server_url=server_url)
    watcher.start()

    # Setup environment
    setup_environment()

    # Get ttyd setup
    zsh_path, claude_path = check_and_get_ttyd_setup()

    try:
        # Start both processes
        server_process = start_server_process(host, port)
        ttyd_process = start_ttyd_process(zsh_path, claude_path)

        # Give the server a moment to start up
        time.sleep(2)

        # Open browser unless --no-open flag is specified
        open_browser_if_requested(host, port, no_open)

        # Wait for either process to exit
        while True:
            server_poll = server_process.poll()
            ttyd_poll = ttyd_process.poll()

            if server_poll is not None:
                click.echo("\nServer process exited.")
                if ttyd_poll is None:
                    ttyd_process.terminate()
                break

            if ttyd_poll is not None:
                click.echo("\nTTYD process exited.")
                if server_poll is None:
                    server_process.terminate()
                break

            time.sleep(1)

    except KeyboardInterrupt:
        click.echo("\nStopping all processes...")
        watcher.stop()
        if server_process.poll() is None:
            server_process.terminate()
            server_process.wait()
        if ttyd_process.poll() is None:
            ttyd_process.terminate()
            ttyd_process.wait()
        click.echo("All processes stopped.")
    except Exception as e:
        click.echo(f"Error: {e}")
        watcher.stop()
        if "server_process" in locals() and server_process.poll() is None:
            server_process.terminate()
        if "ttyd_process" in locals() and ttyd_process.poll() is None:
            ttyd_process.terminate()
        sys.exit(1)


if __name__ == "__main__":
    main()
