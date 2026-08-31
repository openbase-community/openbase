from __future__ import annotations

import sys
from dataclasses import replace

import click

from openbase_coder_cli.services.published_services import (
    PublishedService,
    allocate_ports,
    apply_route,
    gateway_healthy,
    install_launchd_service,
    load_services,
    local_service_available,
    remove_route,
    save_services,
    service_url,
    start_ephemeral_gateway,
    stop_gateway,
    validate_local_port,
    validate_name,
)


@click.group()
def service() -> None:
    """Publish local HTTP services privately over the Openbase VPN."""


def _persistence_choice(persist: bool | None) -> bool:
    if persist is not None:
        return persist
    if sys.stdin.isatty():
        return click.confirm(
            "Start this published service automatically at login with launchd?",
            default=False,
        )
    click.echo("Persistence was not enabled. Use --persist to opt in to launchd.")
    return False


@service.command()
@click.argument("name")
@click.argument("port", type=int)
@click.option(
    "--persist/--no-persist",
    default=None,
    help="Opt in or out of restoring the proxy at login (interactive prompt by default).",
)
@click.option(
    "--tailnet-port",
    type=int,
    help="Uncommon dynamic/private tailnet port; one is chosen automatically.",
)
def publish(
    name: str, port: int, persist: bool | None, tailnet_port: int | None
) -> None:
    """Publish loopback HTTP PORT at a memorable tailnet URL named NAME."""
    try:
        name = validate_name(name)
        port = validate_local_port(port)
        services = load_services()
        if any(item.name == name for item in services):
            raise ValueError(f"Service '{name}' is already published.")
        if not local_service_available(port):
            raise ValueError(
                f"No service is accepting connections on 127.0.0.1:{port}."
            )
        published_port, proxy_port = allocate_ports(tailnet_port)
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    persistent = _persistence_choice(persist)
    service_entry = PublishedService(
        name=name,
        local_port=port,
        tailnet_port=published_port,
        proxy_port=proxy_port,
        persistent=persistent,
    )
    save_services([*services, service_entry])
    try:
        if persistent:
            install_launchd_service(service_entry)
        else:
            pid = start_ephemeral_gateway(service_entry)
            service_entry = replace(service_entry, pid=pid)
            save_services([*services, service_entry])
        if not gateway_healthy(service_entry):
            raise RuntimeError("The local publication gateway did not become healthy.")
        apply_route(service_entry)
        url = service_url(service_entry)
    except Exception as exc:
        stop_gateway(service_entry)
        save_services(services)
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Published {name}: {url}")
    click.echo(f"  Local target: http://127.0.0.1:{port}")
    click.echo("  Visibility: Openbase VPN/tailnet only (never Funnel/public internet)")
    if persistent:
        click.echo("  Persistence: launchd enabled by explicit opt-in")
    else:
        click.echo("  Persistence: current login session only")


@service.command("list")
def list_command() -> None:
    """List published services and their tailnet URLs."""
    try:
        services = load_services()
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if not services:
        click.echo("No local services are published.")
        return
    for item in services:
        try:
            url = service_url(item)
        except RuntimeError:
            url = f"tailnet :{item.tailnet_port}/{item.name}/"
        mode = "launchd" if item.persistent else "session"
        health = "ready" if gateway_healthy(item, timeout=0.1) else "gateway stopped"
        click.echo(
            f"{item.name:<20} {url} -> 127.0.0.1:{item.local_port} ({mode}, {health})"
        )


@service.command()
@click.argument("name")
def unpublish(name: str) -> None:
    """Remove NAME from the tailnet and stop its local gateway."""
    try:
        name = validate_name(name)
        services = load_services()
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    target = next((item for item in services if item.name == name), None)
    if target is None:
        raise click.ClickException(f"Service '{name}' is not published.")
    remaining = [item for item in services if item.name != name]
    save_services(remaining)
    try:
        remove_route(target)
    except Exception as exc:
        save_services(services)
        raise click.ClickException(str(exc)) from exc
    stop_gateway(target)
    click.echo(f"Unpublished {name}.")
