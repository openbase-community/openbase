"""
CLI entry point for openbase_coder_cli.
"""

from __future__ import annotations

import click

from openbase_coder_cli._version import __version__
from openbase_coder_cli.codex_home_instructions import (
    refresh_openbase_agents_md_from_installation,
)

from .auth import auth, login, logout
from .backend import backend
from .boilersync import boilersync
from .bootstrap import bootstrap
from .claude import claude
from .claude_chrome import claude_chrome
from .claude_sync import claude_sync
from .cloud import cloud
from .codex_sync import codex_sync
from .computer_use import computer_use
from .defaults import defaults
from .desktop import desktop
from .doctor import doctor
from .onboarding import onboarding
from .plugins import plugins
from .provision import provision
from .reports import reports
from .restart import restart, self_restart
from .routines import routines
from .self_update import self_update, version_command
from .server import server
from .services import services
from .setup import setup
from .super_agent_name import super_agent_name
from .sync import sync
from .sync_workers import sync_workers
from .user import exit_to_dispatch, user
from .vibes import vibes


def print_version(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    click.echo(f"openbase-coder {__version__}")
    ctx.exit()


@click.group()
@click.option(
    "--version",
    is_flag=True,
    callback=print_version,
    expose_value=False,
    is_eager=True,
    help="Show the version and exit.",
)
def main():
    """Openbase Coder Cli

    OpenBase Coder CLI with embedded server
    """
    refresh_openbase_agents_md_from_installation()


main.add_command(server)
main.add_command(setup)
main.add_command(backend)
main.add_command(services)
main.add_command(doctor)
main.add_command(onboarding)
main.add_command(desktop)
main.add_command(login)
main.add_command(logout)
main.add_command(auth)
main.add_command(plugins)
main.add_command(provision)
main.add_command(reports)
main.add_command(bootstrap)
main.add_command(restart)
main.add_command(self_restart)
main.add_command(user)
main.add_command(boilersync)
main.add_command(cloud)
main.add_command(codex_sync)
main.add_command(claude_sync)
main.add_command(claude)
main.add_command(claude_chrome)
main.add_command(computer_use)
main.add_command(routines)
main.add_command(super_agent_name)
main.add_command(sync)
main.add_command(sync_workers)
main.add_command(defaults)
main.add_command(exit_to_dispatch)
main.add_command(vibes)
main.add_command(self_update)
main.add_command(version_command)


if __name__ == "__main__":
    main()
