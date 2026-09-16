"""CLI commands for onboarding state and cloud device registration."""

from __future__ import annotations

import json

import click

from openbase_coder_cli.services.cloud_registration import register_and_report
from openbase_coder_cli.services.onboarding import onboarding_status_payload


@click.group()
def onboarding() -> None:
    """Onboarding state and openbase-cloud device registration."""


@onboarding.command("status")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print machine-readable JSON instead of a summary.",
)
def onboarding_status_cmd(as_json: bool) -> None:
    """Show local onboarding state (CLI configured, Tailscale, auth)."""
    from openbase_coder_cli.services.tailnet_experience import tailnet_provider_name

    payload = onboarding_status_payload()
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    def line(value: bool, label: str, *, indent: str = "") -> None:
        mark = (
            click.style("  OK  ", fg="green")
            if value
            else click.style("  --  ", fg="yellow")
        )
        click.echo(indent + mark + label)

    line(payload["cli_configured"], "cli_configured")
    for name, value in payload["checks"].items():
        line(value, name, indent="    ")
    line(payload["authenticated"], "authenticated")

    backend_auth = payload["backend_auth"]
    line(backend_auth["ready"], f"backend auth ({backend_auth['backend']})")

    tailscale_self = payload["tailscale_self"]
    dns_name = tailscale_self["dns_name"]
    provider_name = tailnet_provider_name()
    line(
        tailscale_self["available"],
        f"{provider_name} identity" + (f" ({dns_name})" if dns_name else ""),
    )
    if tailscale_self["error"]:
        click.echo(f"        {tailscale_self['error']}")

    serve = payload["tailscale_serve"]
    line(
        serve["healthy"],
        f"{provider_name} routes"
        + (f" ({serve['openbase_url']})" if serve["openbase_url"] else ""),
    )
    if serve["error"]:
        click.echo(f"        {serve['error']}")


@onboarding.command("report")
def onboarding_report_cmd() -> None:
    """Register this device and report CLI state to openbase-cloud."""
    result = register_and_report()
    if result.ok:
        click.echo("Registered device and reported CLI state to openbase-cloud.")
        return
    if not result.supported:
        click.echo(
            click.style(
                f"Skipped: {result.error}",
                fg="yellow",
            )
        )
        return
    raise click.ClickException(result.error or "Cloud report failed.")
