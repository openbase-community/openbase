"""``openbase-coder tailnet`` — select the tailnet provider.

coder can ride on the official Tailscale client, on Openbase's self-hosted
netmesh (headscale + hardened macOS/iOS VPN client), or on netmesh joined by an
in-process embedded node (no VPN on either side; ``netmesh-tsnet``). Only one is
active at a time; it is chosen at setup (``OPENBASE_CODER_CLI_TAILSCALE_PROVIDER``)
and switched here.
"""

from __future__ import annotations

import click

from openbase_coder_cli.cli.utils import get_data_dir
from openbase_coder_cli.env_file import env_file_values, upsert_env_file_values
from openbase_coder_cli.services import tailscale_provider as tp

PROVIDER_ENV_KEY = "OPENBASE_CODER_CLI_TAILSCALE_PROVIDER"
ALLOWED_HOSTS_ENV_KEY = "OPENBASE_CODER_CLI_ALLOWED_HOSTS"
NETMESH_ALLOWED_SUFFIX = ".netmesh.openbase.cloud"
LIVEKIT_MODE_ENV_KEY = "LIVEKIT_NETWORK_MODE"


def _env_path():
    return get_data_dir() / ".env"


@click.group()
def tailnet() -> None:
    """Select the tailnet provider (official Tailscale or Openbase netmesh)."""


@tailnet.command("set-provider")
@click.argument("name", type=click.Choice(list(tp.PROVIDER_VALUES)))
def set_provider(name: str) -> None:
    """Switch the active tailnet provider.

    When switching to either netmesh transport, the netmesh MagicDNS suffix is
    added to the allowed hosts so served requests (which arrive with a netmesh
    Host header) are accepted by the backend.
    """
    path = _env_path()
    values: dict[str, str] = {PROVIDER_ENV_KEY: name}

    if name in (tp.PROVIDER_NETMESH, tp.PROVIDER_NETMESH_TSNET):
        existing = env_file_values(path)
        hosts = existing.get(ALLOWED_HOSTS_ENV_KEY, "localhost,127.0.0.1,.ts.net")
        host_list = [h.strip() for h in hosts.split(",") if h.strip()]
        if NETMESH_ALLOWED_SUFFIX not in host_list:
            host_list.append(NETMESH_ALLOWED_SUFFIX)
            values[ALLOWED_HOSTS_ENV_KEY] = ",".join(host_list)

    # Embedded mode: all media reaches LiveKit host-locally through the tunneld
    # TURN relay, so LiveKit must run in "local" network mode. With --node-ip
    # set (tailscale mode), every advertised candidate collapses to that IP,
    # the loopback candidate is never sent, the phone never installs a TURN
    # permission for 127.0.0.1, and the server's host-local checks (which
    # arrive with a loopback source) are all dropped by the relay.
    values[LIVEKIT_MODE_ENV_KEY] = (
        "local" if name == tp.PROVIDER_NETMESH_TSNET else "tailscale"
    )

    upsert_env_file_values(path, values)
    click.echo(f"Tailnet provider set to '{name}' in {path}.")
    if name == tp.PROVIDER_NETMESH_TSNET:
        click.echo(
            "Note: embedded no-VPN transport needs the openbase-tunneld daemon, "
            "which is staged separately — control/serve ops report pending until "
            "it lands."
        )
    click.echo(
        "Restart coder services to apply (e.g. `openbase-coder services restart`)."
    )


@tailnet.command("show")
def show() -> None:
    """Show the configured provider and whether its control tool is present."""
    configured = env_file_values(_env_path()).get(
        PROVIDER_ENV_KEY, tp.PROVIDER_TAILSCALE
    )
    click.echo(f"configured provider: {configured}")
    click.echo(f"active provider:     {tp.provider()}")
    click.echo(f"control tool:        {tp.tool_path() or '(not found)'}")
