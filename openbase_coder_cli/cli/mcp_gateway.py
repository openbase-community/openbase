"""``openbase-coder mcp-gateway`` — reach a laptop-bound MCP server from a hub.

Laptop side: ``serve add|remove|list`` chooses which local stdio MCP servers
other devices of the same owner may open. Hub side: ``offer`` lists one of
them in this machine's agent profiles as ``<name>-laptop``; the agent decides
whether to call it. ``connect`` is the stdio server those profiles run.
"""

from __future__ import annotations

import asyncio
import sys

import click

from openbase_coder_cli import mcp_gateway as gw


@click.group("mcp-gateway")
def mcp_gateway() -> None:
    """Make MCP servers bound to one machine available to agents on another."""


@mcp_gateway.group("serve")
def serve() -> None:
    """Choose which local MCP servers your other devices may use (laptop side)."""


@serve.command(
    "add",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.argument("name")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
@click.option("--description", default="", help="Shown by `serve list`.")
def serve_add_command(name: str, command: tuple[str, ...], description: str) -> None:
    """Serve NAME: a built-in server, or any stdio MCP COMMAND given after `--`.

    \b
    Examples:
      openbase-coder mcp-gateway serve add computer
      openbase-coder mcp-gateway serve add browser -- npx some-browser-mcp
    """
    argv = list(command)
    if argv and argv[0] == "--":
        argv = argv[1:]
    try:
        server = gw.serve_add(name, argv or None, description)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Serving {server.name}: {' '.join(server.command)}")
    click.echo(
        "Your other devices can now offer it to their agents with:\n"
        f"  openbase-coder mcp-gateway offer {server.name} --peer <this device>"
    )


@serve.command("remove")
@click.argument("name")
def serve_remove_command(name: str) -> None:
    """Stop serving NAME (open bridges end when their socket closes)."""
    if gw.serve_remove(name):
        click.echo(f"No longer serving {name}.")
    else:
        click.echo(f"{name} was not served.")


@serve.command("list")
def serve_list_command() -> None:
    """Servers this machine serves, and the built-ins it could serve."""
    servers = gw.served_servers()
    if servers:
        for server in servers.values():
            line = f"{server.name}: {' '.join(server.command)}"
            if server.description:
                line += f"  ({server.description})"
            click.echo(line)
    else:
        click.echo("Not serving any MCP servers.")
    available = [name for name in gw.BUILTIN_SERVERS if name not in servers]
    if available:
        click.echo(
            "Built-in, not served: "
            + ", ".join(available)
            + " (serve with `openbase-coder mcp-gateway serve add <name>`)"
        )


@mcp_gateway.command("connect")
@click.argument("name")
@click.option(
    "--peer", required=True, help="Device name (or tailnet host) serving NAME."
)
@click.option(
    "--timeout",
    default=5.0,
    show_default=True,
    type=float,
    help="Seconds to wait for the peer before giving up.",
)
def connect_command(name: str, peer: str, timeout: float) -> None:
    """Stdio MCP server relaying to NAME on PEER (run by agents, not people).

    Exits 69 at once when the peer is offline, not signed in to the same
    account, or does not serve NAME, so the agent sees the server as
    unavailable instead of waiting.
    """
    from openbase_coder_cli.services.fleet_aggregation import (
        find_peer,
        owner_access_token,
    )

    def unavailable(message: str) -> None:
        click.echo(f"mcp-gateway: {message}", err=True)
        sys.exit(gw.EXIT_UNAVAILABLE)

    target = find_peer(peer)
    if target is None:
        unavailable(f"{peer} is not online on your Openbase network")
    token = owner_access_token()
    if not token:
        unavailable("not signed in to Openbase Cloud; cannot authenticate to the peer")
    url = gw.peer_ws_url(target.base_url, name, token)
    sys.exit(asyncio.run(gw.relay_stdio(url, open_timeout=timeout)))


@mcp_gateway.command("offer")
@click.argument("name")
@click.option(
    "--peer", required=True, help="Device that serves NAME (e.g. your laptop)."
)
def offer_command(name: str, peer: str) -> None:
    """List NAME-laptop in this machine's agent profiles (hub side).

    This only makes the server available. Agents decide whether to call it;
    nothing is routed through it automatically. New sessions pick it up.
    """
    try:
        changed = gw.offer(name, peer)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    key = gw.offered_name(name)
    if changed:
        for path in changed:
            click.echo(f"Offered {key} (on {peer}) in {path}")
    else:
        click.echo(f"{key} was already offered.")


@mcp_gateway.command("withdraw")
@click.argument("name")
def withdraw_command(name: str) -> None:
    """Remove NAME-laptop from this machine's agent profiles."""
    try:
        changed = gw.withdraw(name)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    key = gw.offered_name(name)
    if changed:
        for path in changed:
            click.echo(f"Withdrew {key} from {path}")
    else:
        click.echo(f"{key} was not offered.")


@mcp_gateway.command("offered")
def offered_command() -> None:
    """Gateway servers offered to this machine's agents."""
    servers = gw.offered()
    if not servers:
        click.echo("No gateway servers offered.")
        return
    for server in servers.values():
        profiles = ", ".join(str(p) for p in server.profiles)
        click.echo(f"{server.key}: {server.name} on {server.peer or '?'}  [{profiles}]")
