from __future__ import annotations

import click

from openbase_coder_cli.claude_auth import run_claude_login, verified_claude_auth_status
from openbase_coder_cli.cli.agents import (
    LAUNCHER_HELP,
    AgentLauncherGroup,
    launch_from_group,
)


@click.group(
    cls=AgentLauncherGroup,
    invoke_without_command=True,
    epilog=LAUNCHER_HELP,
)
@click.pass_context
def claude(ctx: click.Context) -> None:
    """Start Claude Code with Openbase's profile, or manage its login.

    `openbase-coder claude [ARGS...]` starts Claude Code with Openbase's
    settings and MCP layer (`~/.openbase/profiles/claude`) and Openbase's
    instructions, so the session is visible to and steerable from Openbase.
    ARGS go to Claude Code unchanged. Plain `claude` is left untouched.

    The subcommands below inspect and repair the shared Claude Code login.
    """
    launch_from_group(ctx, "claude")


@claude.command()
def status() -> None:
    """Show Claude Code auth status for the shared ~/.claude login."""
    result = verified_claude_auth_status()
    click.echo(result.raw_output)
    if not result.logged_in:
        raise click.ClickException(
            "Claude Code is not logged in. Run `claude login` (or "
            "`openbase-coder claude login`)."
        )


@claude.command()
@click.option("--sso", is_flag=True, help="Force Claude SSO login flow.")
@click.option("--email", default=None, help="Pre-populate the Claude login email.")
def login(sso: bool, email: str | None) -> None:
    """Run the Claude Code login used by Openbase sessions."""
    raise SystemExit(run_claude_login(sso=sso, email=email))


@claude.command("computer-use-mcp", hidden=True)
def computer_use_mcp() -> None:
    """Run the Openbase computer-use MCP server on stdio (desktop-app proxy)."""
    from openbase_coder_cli.claude_computer_use_mcp import main as mcp_main

    mcp_main()
