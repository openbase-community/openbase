"""``openbase-coder codex`` / ``openbase-coder claude``: launch an agent the Openbase way.

Both launch the real CLI with Openbase's session profile so the session is
visible to (and steerable from) Openbase. On an Openbase Sync edge whose
current folder is synced and whose hub is reachable, the session runs on the
hub and this terminal attaches to it; everywhere else it runs right here.
Plain ``codex`` / ``claude`` are never changed.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

import click

from openbase_coder_cli.agent_launch import (
    AgentLaunchError,
    default_launch_context,
    is_interactive_session,
    plan_agent_launch,
)
from openbase_coder_cli.agent_remote import (
    LOCAL,
    REMOTE,
    RemoteUnavailableError,
    home_relative,
    read_sync_facts,
    run_remote,
    select_mode,
)

LAUNCHER_FLAGS = {"--local": LOCAL, "--remote": REMOTE}

LAUNCHER_HELP = """\
Openbase options (they must come first; everything after them goes to the
agent unchanged):

\b
  --local    Run on this computer even when it is a paired Openbase Sync edge.
  --remote   Run on the Openbase Sync hub; fail instead of running locally.

Without either flag the session runs on the hub when this computer is a
paired edge, the current folder is synced, and the hub is reachable;
otherwise it runs here.
"""


def parse_launcher_args(args: Sequence[str]) -> tuple[str | None, list[str]]:
    """Split leading ``--local`` / ``--remote`` from the agent's own args."""
    force: str | None = None
    rest = list(args)
    while rest and rest[0] in LAUNCHER_FLAGS:
        mode = LAUNCHER_FLAGS[rest.pop(0)]
        if force is not None and force != mode:
            raise click.UsageError("Use either --local or --remote, not both.")
        force = mode
    return force, rest


def _echo(line: str) -> None:
    click.echo(line, err=True)


def run_local(agent: str, args: Sequence[str], cwd: Path) -> NoReturn:
    try:
        launch = plan_agent_launch(
            agent, args, cwd, default_launch_context(), base_env=os.environ
        )
    except AgentLaunchError as exc:
        raise click.ClickException(str(exc)) from exc
    for notice in launch.notices:
        _echo(notice)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execve(launch.argv[0], launch.argv, launch.env)
    except OSError as exc:
        raise click.ClickException(f"Unable to start {launch.argv[0]}: {exc}") from exc


def launch_agent(agent: str, raw_args: Sequence[str]) -> NoReturn:
    force, args = parse_launcher_args(raw_args)
    cwd = Path.cwd()
    home = Path.home()
    try:
        decision = select_mode(
            force=force,
            facts=read_sync_facts(),
            cwd=cwd,
            home=home,
            interactive=is_interactive_session(agent, args),
        )
    except RemoteUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
    if decision.reason:
        _echo(decision.reason)
    if decision.mode == REMOTE and decision.hub_url:
        _run_on_hub(agent, args, cwd, home, decision.hub_url, forced=force == REMOTE)
    run_local(agent, args, cwd)


def _run_on_hub(
    agent: str,
    args: Sequence[str],
    cwd: Path,
    home: Path,
    hub_url: str,
    *,
    forced: bool,
) -> None:
    """Run on the hub and exit with its code; return only to fall back."""
    from openbase_coder_cli.services.fleet_aggregation import owner_access_token

    token = owner_access_token()
    if not token:
        message = "Not signed in to Openbase, so the hub can't be used"
        if forced:
            raise click.ClickException(f"{message}. Run `openbase-coder login`.")
        _echo(f"{message}; running locally.")
        return

    def on_ready(notices: list[str]) -> None:
        _echo(f"Running on the Openbase Sync hub ({hub_url}).")
        for notice in notices:
            _echo(f"Hub: {notice}")

    try:
        code = run_remote(
            base_url=hub_url,
            token=token,
            agent=agent,
            cwd=home_relative(cwd, home),
            args=args,
            on_ready=on_ready,
        )
    except RemoteUnavailableError as exc:
        if forced:
            raise click.ClickException(str(exc)) from exc
        _echo(f"{str(exc).rstrip('.')}. Running locally.")
        return
    raise SystemExit(code)


_PASSTHROUGH = {
    "ignore_unknown_options": True,
    "allow_extra_args": True,
    "allow_interspersed_args": False,
    "help_option_names": [],
}


@click.command(
    "codex",
    context_settings=_PASSTHROUGH,
    epilog=LAUNCHER_HELP,
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def codex(ctx: click.Context, args: tuple[str, ...]) -> None:
    """Start Codex with Openbase's profile, visible to Openbase.

    Runs `codex -p openbase` (`-p openbase-cloud` on an Openbase Cloud
    backend) attached to this computer's Openbase-managed Codex app-server,
    so the dispatcher and the phone can see and steer the session. Plain
    `codex` is left untouched. `openbase-coder codex --help` shows this text;
    run plain `codex --help` for Codex's own options.
    """
    if args[:1] == ("--help",):
        click.echo(ctx.get_help())
        return
    launch_agent("codex", args)


class AgentLauncherGroup(click.Group):
    """A command group whose non-subcommand arguments launch the agent.

    ``openbase-coder claude status`` still runs the ``status`` subcommand;
    ``openbase-coder claude -c`` (or any other non-subcommand argument, or
    none) launches Claude Code with those arguments.
    """

    LAUNCH_ARGS_KEY = "agent_launch_args"

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and (args[0] in self.commands or args[0] in ctx.help_option_names):
            return super().parse_args(ctx, args)
        ctx.meta[self.LAUNCH_ARGS_KEY] = list(args)
        return super().parse_args(ctx, [])


def launch_from_group(ctx: click.Context, agent: str) -> None:
    if ctx.invoked_subcommand is not None:
        return
    launch_agent(agent, ctx.meta.get(AgentLauncherGroup.LAUNCH_ARGS_KEY, []))
