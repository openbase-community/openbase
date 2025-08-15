"""TTYd terminal server command for Openbase CLI."""

import subprocess
import sys
from pathlib import Path

import click


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


@click.command()
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