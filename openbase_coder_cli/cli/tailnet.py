"""``openbase-coder tailnet`` — select and manage the tailnet transport.

coder can ride on the official Tailscale client, on Openbase's self-hosted
netmesh (headscale + hardened macOS/iOS VPN client), or on netmesh joined by an
in-process embedded node (no VPN on either side; ``netmesh-tsnet``). The three
transports are three different networks, so a user's whole device fleet must
agree: the account-level choice lives in openbase-cloud (surfaced through
onboarding state) and the local env var is this machine's materialization of it.
"""

from __future__ import annotations

import subprocess

import click

from openbase_coder_cli.cli.utils import get_data_dir
from openbase_coder_cli.env_file import env_file_values, upsert_env_file_values
from openbase_coder_cli.services import tailscale_provider as tp

PROVIDER_ENV_KEY = "OPENBASE_CODER_CLI_TAILSCALE_PROVIDER"
ALLOWED_HOSTS_ENV_KEY = "OPENBASE_CODER_CLI_ALLOWED_HOSTS"
NETMESH_ALLOWED_SUFFIX = ".netmesh.openbase.cloud"
LIVEKIT_MODE_ENV_KEY = "LIVEKIT_NETWORK_MODE"

# Pre-integration LaunchAgent label for tunneld; superseded by the managed
# openbase-tunneld service, cleaned up on any provider switch.
LEGACY_TUNNELD_AGENT = "cloud.openbase.tunneld"

# Services whose behaviour depends on the transport (LiveKit's rtc candidate
# mode, the backend's serve/status probing, the agent's LiveKit connection).
_RESTART_SERVICE_NAMES = ("livekit-server", "livekit-agent", "django-cli")


def _env_path():
    return get_data_dir() / ".env"


def _configured_provider() -> str:
    return env_file_values(_env_path()).get(PROVIDER_ENV_KEY, tp.PROVIDER_TAILSCALE)


@click.group()
def tailnet() -> None:
    """Select the tailnet transport (Tailscale, netmesh VPN, or embedded)."""


@tailnet.command("set-provider")
@click.argument("name", type=click.Choice(list(tp.PROVIDER_VALUES)))
@click.option(
    "--no-cloud",
    is_flag=True,
    help=(
        "Only switch this machine; do not record the choice as the "
        "account-level transport (used by `tailnet sync`)."
    ),
)
def set_provider(name: str, no_cloud: bool) -> None:
    """Switch the active tailnet transport for this machine (and the account).

    Writes the local env, records the account-level choice in openbase-cloud
    (other devices follow via onboarding state), tears down the previous
    transport, brings up the new one, restarts the transport-dependent
    services, and re-registers this device.
    """
    _apply_provider(name, push_cloud=not no_cloud)


def _apply_provider(name: str, *, push_cloud: bool) -> None:
    path = _env_path()
    previous = _configured_provider()

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

    if push_cloud:
        from openbase_coder_cli.services.cloud_registration import (
            push_tailnet_provider,
        )

        result = push_tailnet_provider(name)
        if result.ok:
            click.echo(
                "Recorded as the account-level transport; your other devices "
                "will prompt to follow."
            )
        elif not result.supported:
            click.echo(
                click.style(
                    "Note: openbase-cloud does not support the account-level "
                    "transport yet — other devices will not follow "
                    "automatically.",
                    fg="yellow",
                )
            )
        else:
            click.echo(
                click.style(
                    f"Warning: could not record the choice in openbase-cloud: "
                    f"{result.error}",
                    fg="yellow",
                )
            )

    # Always: the pre-integration LaunchAgent must never survive a switch —
    # even a same-provider re-apply — or it holds tunneld's control port and
    # the managed service crashloops behind it.
    _bootout_legacy_tunneld_agent()
    if previous != name:
        _teardown_transport(previous)
    _bring_up_transport(name)
    _restart_transport_services()
    _reregister_device()

    if name == tp.PROVIDER_NETMESH:
        if tp.netmesh_uses_stock_tailscale():
            click.echo(
                "Next: this machine joins the netmesh through the official "
                "Tailscale client pointed at Openbase's control server."
            )
        else:
            click.echo(
                "Next: connect the Openbase VPN from the Openbase desktop "
                "app (Prerequisites → Openbase VPN)."
            )
    elif name == tp.PROVIDER_TAILSCALE:
        click.echo("Next: make sure the Tailscale app is running and signed in.")


def _teardown_transport(previous: str) -> None:
    """Best-effort teardown of the transport we're leaving."""
    from openbase_coder_cli.services.definitions import TUNNELD_SERVICE
    from openbase_coder_cli.services.launchd import launchctl_bootout

    if previous == tp.PROVIDER_NETMESH_TSNET:
        try:
            launchctl_bootout(TUNNELD_SERVICE)
            click.echo("Stopped openbase-tunneld.")
        except Exception as exc:  # noqa: BLE001 - best-effort teardown
            click.echo(f"Note: could not stop openbase-tunneld: {exc}")
    elif previous == tp.PROVIDER_TAILSCALE:
        tailscale_bin = tp.tailscale_bin()
        if tailscale_bin:
            subprocess.run(  # noqa: S603 - fixed argv, best-effort
                [tailscale_bin, "serve", "reset"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            click.echo("Reset Tailscale Serve rules.")
    elif previous == tp.PROVIDER_NETMESH:
        click.echo(
            "You can disconnect the Openbase VPN from the Openbase desktop "
            "app; its serve rules are no longer used."
        )


def _bootout_legacy_tunneld_agent() -> None:
    """Remove the pre-integration tunneld LaunchAgent if it is still loaded."""
    import os

    subprocess.run(  # noqa: S603 - fixed argv, best-effort
        ["launchctl", "bootout", f"gui/{os.getuid()}/{LEGACY_TUNNELD_AGENT}"],
        capture_output=True,
        timeout=10,
        check=False,
    )


def _bring_up_transport(name: str) -> None:
    if name == tp.PROVIDER_NETMESH and tp.netmesh_uses_stock_tailscale():
        _join_netmesh_with_stock_tailscale()
        _apply_serve_best_effort()
        return
    if name != tp.PROVIDER_NETMESH_TSNET:
        _apply_serve_best_effort()
        return

    from openbase_coder_cli.services.definitions import TUNNELD_SERVICE
    from openbase_coder_cli.services.installation import InstallationConfig
    from openbase_coder_cli.services.launchd import (
        install_service,
        launchctl_kickstart,
    )
    from openbase_coder_cli.services.tunneld import ensure_tunneld_running

    try:
        config = InstallationConfig.load()
        install_service(config, TUNNELD_SERVICE)
        launchctl_kickstart(TUNNELD_SERVICE)
    except Exception as exc:  # noqa: BLE001 - surface, don't crash the switch
        click.echo(
            click.style(
                f"Warning: could not install openbase-tunneld: {exc}", fg="yellow"
            )
        )
        return
    try:
        # Waits for Running + forwards; mints a netmesh key with the user's
        # cloud login if the node still needs one.
        ensure_tunneld_running()
        click.echo("openbase-tunneld is running and joined the tailnet.")
    except RuntimeError as exc:
        click.echo(click.style(f"Warning: {exc}", fg="yellow"))


def _join_netmesh_with_stock_tailscale() -> None:
    """Windows/Linux netmesh VPN: log the official client into our headscale.

    There is no hardened netmesh client off macOS; the same self-hosted
    control plane is joined with ``tailscale login --login-server`` and a
    cloud-minted single-use key.
    """
    from openbase_coder_cli.services.cloud_registration import netmesh_enroll

    tailscale_bin = tp.tailscale_bin()
    if not tailscale_bin:
        click.echo(
            click.style(
                "Warning: the tailscale client is not installed. Install it "
                "from tailscale.com, then rerun "
                "'openbase-coder tailnet set-provider netmesh'.",
                fg="yellow",
            )
        )
        return
    enrollment = netmesh_enroll()
    if not enrollment:
        click.echo(
            click.style(
                "Warning: could not mint a netmesh key (run "
                "'openbase-coder login' first). Join manually with: "
                "tailscale login --login-server https://net.openbase.cloud",
                fg="yellow",
            )
        )
        return
    result = subprocess.run(  # noqa: S603 - fixed argv
        [
            tailscale_bin,
            "login",
            "--login-server",
            enrollment["control_url"],
            "--auth-key",
            enrollment["auth_key"],
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        click.echo(
            click.style(
                "Warning: tailscale login against the netmesh control server "
                f"failed: {result.stderr.strip() or result.stdout.strip()}",
                fg="yellow",
            )
        )
        return
    click.echo("Joined the netmesh with the official Tailscale client.")


def _apply_serve_best_effort() -> None:
    from openbase_coder_cli.services.tailscale_serve import configure_tailscale_serve

    try:
        configure_tailscale_serve()
    except Exception as exc:  # noqa: BLE001 - the doctor/health surfaces this too
        click.echo(f"Note: serve rules not applied yet ({exc}).")


def _restart_transport_services() -> None:
    from openbase_coder_cli.services.definitions import SERVICES
    from openbase_coder_cli.services.launchd import launchctl_kickstart

    for service_name in _RESTART_SERVICE_NAMES:
        service = next((s for s in SERVICES if s.name == service_name), None)
        if service is None:
            continue
        try:
            launchctl_kickstart(service)
        except Exception:  # noqa: BLE001 - service may not be installed
            pass
    click.echo("Restarted transport-dependent services.")


def _reregister_device() -> None:
    from openbase_coder_cli.services.cloud_registration import register_and_report

    result = register_and_report()
    if result.ok:
        click.echo("Re-registered this device with the new tailnet identity.")
    else:
        click.echo(
            click.style(
                f"Note: device re-registration pending: {result.error}", fg="yellow"
            )
        )


@tailnet.command("enroll")
@click.option(
    "--json",
    "json_",
    is_flag=True,
    help="Print the enrollment as JSON without redeeming it (for the desktop "
    "app's netmesh companion).",
)
def enroll(json_: bool) -> None:
    """Join this machine to the user's netmesh with a cloud-minted key.

    Requires `openbase-coder login`. In embedded (netmesh-tsnet) mode the key
    is redeemed by the tunneld daemon automatically; in VPN mode the key is
    handed to whatever drives the VPN (the desktop app's netmesh companion
    consumes the --json form).
    """
    import json as json_module

    from openbase_coder_cli.services.cloud_registration import netmesh_enroll

    enrollment = netmesh_enroll()
    if not enrollment:
        raise click.ClickException(
            "Could not mint a netmesh key. Run 'openbase-coder login' first "
            "and check that openbase-cloud is reachable."
        )
    if json_:
        click.echo(
            json_module.dumps(
                {
                    "control_url": enrollment["control_url"],
                    "auth_key": enrollment["auth_key"],
                }
            )
        )
        return
    if tp.is_netmesh_tsnet():
        from openbase_coder_cli.services.tunneld import ensure_tunneld_running

        ensure_tunneld_running(auth_key=enrollment["auth_key"])
        click.echo("Enrolled: the embedded node joined the netmesh.")
        return
    click.echo(f"control url: {enrollment['control_url']}")
    click.echo(f"auth key (single-use): {enrollment['auth_key']}")
    click.echo("Use these to join the netmesh VPN.")


@tailnet.command("sync")
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Switch this machine to the account-level transport if it differs.",
)
def sync(apply_: bool) -> None:
    """Compare this machine's transport with the account-level choice."""
    from openbase_coder_cli.services.cloud_registration import fetch_tailnet_provider

    cloud = fetch_tailnet_provider()
    local = _configured_provider()
    click.echo(f"account transport: {cloud or '(unavailable)'}")
    click.echo(f"this machine:      {local}")
    if cloud is None or cloud == local:
        return
    if not apply_:
        click.echo(
            f"Run 'openbase-coder tailnet sync --apply' to switch this "
            f"machine to '{cloud}'."
        )
        return
    _apply_provider(cloud, push_cloud=False)


@tailnet.command("show")
def show() -> None:
    """Show the configured provider and whether its control tool is present."""
    configured = _configured_provider()
    click.echo(f"configured provider: {configured}")
    click.echo(f"active provider:     {tp.provider()}")
    click.echo(f"control tool:        {tp.tool_path() or '(not found)'}")


@tailnet.command("status")
@click.option("--json", "json_", is_flag=True, help="Print the raw status JSON.")
def status(json_: bool) -> None:
    """Live tailnet connectivity: backend state, this node, and per-peer path.

    The CLI equivalent of the menu-bar readout: whether this machine is
    actually joined to the network (any provider — Tailscale app, netmesh
    VPN, or the embedded no-VPN node) and whether each peer is reachable
    direct or via a DERP relay.
    """
    import json as json_module

    payload = tp.status_json()
    if json_:
        click.echo(json_module.dumps(payload, indent=2))
        return
    if error := payload.get("error"):
        raise click.ClickException(f"{tp.provider()}: {error}")

    state = payload.get("BackendState") or "unknown"
    color = {"Running": "green", "Starting": "yellow"}.get(state, "red")
    click.echo(f"provider: {tp.provider()}")
    click.echo("state:    " + click.style(state, fg=color))

    self_node = payload.get("Self") or {}
    dns = str(self_node.get("DNSName") or "").rstrip(".")
    ips = ", ".join(self_node.get("TailscaleIPs") or [])
    if dns or ips:
        click.echo(f"this node: {dns or '(no name)'}  {ips}")

    peers = list((payload.get("Peer") or {}).values())
    if not peers:
        click.echo("peers: none visible")
        return
    click.echo("peers:")
    for peer in sorted(peers, key=lambda p: str(p.get("HostName") or "")):
        name = str(peer.get("HostName") or "?")
        ip4 = next((ip for ip in peer.get("TailscaleIPs") or [] if "." in ip), "")
        if not peer.get("Online"):
            path = click.style("offline", fg="red")
        elif peer.get("CurAddr"):
            path = click.style(f"direct {peer['CurAddr']}", fg="green")
        elif peer.get("Relay"):
            path = click.style(f"relay {peer['Relay']}", fg="yellow")
        else:
            path = "idle"
        click.echo(f"  {name:<32} {ip4:<16} {path}")
