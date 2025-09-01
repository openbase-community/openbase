"""Ungit Git UI server command for Openbase CLI."""

import os
import subprocess
import sys
from pathlib import Path

import click


def check_ungit_installation():
    """Check if ungit is installed globally."""
    try:
        subprocess.run(["which", "ungit"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        click.echo("Error: ungit is not installed or not in PATH")
        click.echo("Install ungit with: npm install -g ungit")
        sys.exit(1)


def build_ungit_command(port=8448):
    """Build the ungit command array."""
    cmd = [
        "ungit",
        "--port",
        str(port),
        "--launchBrowser=false",
        "--bugtracking=false",
        "--isAnimate=false",
        "--ungitBindIp=0.0.0.0",
        "--authentication=true",
        "--users.openbase=openbase123",
        "--logLevel=debug",
    ]
    return cmd


@click.command()
@click.option("--port", "-p", default=8448, help="Port to run ungit on (default: 8448)")
def ungit(port):
    """Start ungit Git UI server."""
    click.echo(f"Starting ungit Git UI server on port {port}...")

    # Check if ungit is installed
    check_ungit_installation()

    # Ensure we're in a git repository
    if not Path(".git").exists():
        click.echo("Warning: Not in a git repository")

    try:
        cwd = Path(os.environ.get("OPENBASE_PROJECT_DIR", ".")).resolve()
        cmd = build_ungit_command(port)
        click.echo(f"Running: {' '.join(cmd)}")
        click.echo(f"Running in: {cwd}")
        subprocess.run(cmd, check=True, cwd=cwd)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error running ungit: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo(f"\nUngit server on port {port} stopped.")
