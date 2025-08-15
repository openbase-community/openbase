"""Default command that runs both server and ttyd for Openbase CLI."""

import sys
import time

import click

from .watcher import DirectoryWatcher
from .server import start_server_process
from .ttyd import check_and_get_ttyd_setup, start_ttyd_process
from .utils import setup_environment, open_browser


@click.command()
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
        if not no_open:
            open_browser(host, port)

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