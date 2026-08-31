from __future__ import annotations

import sys
from dataclasses import replace
from functools import wraps

import click

from openbase_coder_cli.services.published_service_routes import (
    apply_route,
    ensure_portless_capability,
    remove_route,
)
from openbase_coder_cli.services.published_services import (
    MODE_DYNAMIC,
    MODE_PORTLESS,
    PORTLESS_TAILNET_PORT,
    PublishedService,
    ServiceRegistry,
    allocate_portless_proxy,
    allocate_ports,
    gateway_healthy,
    install_launchd_service,
    load_registry,
    local_service_available,
    registry_lock,
    save_registry,
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


def _registry_transaction(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with registry_lock():
            return function(*args, **kwargs)

    return wrapped


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
@click.option(
    "--mode",
    type=click.Choice([MODE_DYNAMIC, MODE_PORTLESS], case_sensitive=False),
    default=None,
    help="Publication mode; dynamic remains the default.",
)
@click.option(
    "--portless",
    is_flag=True,
    help="Alias for --mode portless; publishes under /services/NAME/ on HTTP 80.",
)
@_registry_transaction
def publish(
    name: str,
    port: int,
    persist: bool | None,
    tailnet_port: int | None,
    mode: str | None,
    portless: bool,
) -> None:
    """Publish loopback HTTP PORT at a memorable tailnet URL named NAME."""
    try:
        if portless and mode == MODE_DYNAMIC:
            raise ValueError("--portless conflicts with --mode dynamic.")
        publication_mode = MODE_PORTLESS if portless else (mode or MODE_DYNAMIC)
        name = validate_name(name)
        port = validate_local_port(port)
        registry = load_registry()
        services = list(registry.services)
        if any(item.name == name for item in services):
            raise ValueError(f"Service '{name}' is already published.")
        if not local_service_available(port):
            raise ValueError(
                f"No service is accepting connections on 127.0.0.1:{port}."
            )
        if publication_mode == MODE_PORTLESS:
            ensure_portless_capability()
            if tailnet_port is not None:
                raise ValueError("--tailnet-port cannot be used with portless mode.")
            published_port = PORTLESS_TAILNET_PORT
            proxy_port = allocate_portless_proxy()
        else:
            published_port, proxy_port = allocate_ports(tailnet_port)
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    persistent = _persistence_choice(persist)
    existing_portless = next(
        (item for item in services if item.mode == MODE_PORTLESS), None
    )
    if (
        publication_mode == MODE_PORTLESS
        and existing_portless is not None
        and existing_portless.persistent != persistent
    ):
        raise click.ClickException(
            "All services on the shared portless dispatcher must use the same "
            "persistence setting. Republish with the existing setting."
        )
    service_entry = PublishedService(
        name=name,
        local_port=port,
        tailnet_port=published_port,
        proxy_port=proxy_port,
        persistent=persistent,
        pid=existing_portless.pid if existing_portless else None,
        mode=publication_mode,
    )
    desired_services = [*services, service_entry]
    save_registry(
        ServiceRegistry(tuple(desired_services), registry.last_applied_serve_hash)
    )
    try:
        if existing_portless is None or publication_mode == MODE_DYNAMIC:
            if persistent:
                install_launchd_service(service_entry)
            else:
                pid = start_ephemeral_gateway(service_entry)
                service_entry = replace(service_entry, pid=pid)
                desired_services[-1] = service_entry
                save_registry(
                    ServiceRegistry(
                        tuple(desired_services), registry.last_applied_serve_hash
                    )
                )
        if not gateway_healthy(service_entry):
            raise RuntimeError("The local publication gateway did not become healthy.")
        applied_hash = apply_route(
            service_entry,
            previous_services=services,
            desired_services=desired_services,
            last_applied_hash=registry.last_applied_serve_hash,
        )
        save_registry(ServiceRegistry(tuple(desired_services), applied_hash))
        url = service_url(service_entry)
    except Exception as exc:
        if existing_portless is None or publication_mode == MODE_DYNAMIC:
            stop_gateway(service_entry)
        save_registry(registry)
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Published {name}: {url}")
    click.echo(f"  Local target: http://127.0.0.1:{port}")
    click.echo(f"  Mode: {publication_mode}")
    click.echo("  Visibility: Openbase VPN/tailnet only (never Funnel/public internet)")
    if persistent:
        click.echo("  Persistence: launchd enabled by explicit opt-in")
    else:
        click.echo("  Persistence: current login session only")


@service.command("list")
def list_command() -> None:
    """List published services and their tailnet URLs."""
    try:
        services = list(load_registry().services)
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
            f"{item.name:<20} {url} -> 127.0.0.1:{item.local_port} "
            f"({item.mode}, {mode}, {health})"
        )


@service.command()
@click.argument("name")
@_registry_transaction
def unpublish(name: str) -> None:
    """Remove NAME from the tailnet and stop its local gateway."""
    try:
        name = validate_name(name)
        registry = load_registry()
        services = list(registry.services)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    target = next((item for item in services if item.name == name), None)
    if target is None:
        raise click.ClickException(f"Service '{name}' is not published.")
    if target.mode == MODE_PORTLESS:
        try:
            ensure_portless_capability()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
    remaining = [item for item in services if item.name != name]
    save_registry(ServiceRegistry(tuple(remaining), registry.last_applied_serve_hash))
    try:
        applied_hash = remove_route(
            target,
            previous_services=services,
            desired_services=remaining,
            last_applied_hash=registry.last_applied_serve_hash,
        )
    except Exception as exc:
        save_registry(registry)
        raise click.ClickException(str(exc)) from exc
    save_registry(ServiceRegistry(tuple(remaining), applied_hash))
    shared_dispatcher_still_needed = target.mode == MODE_PORTLESS and any(
        item.mode == MODE_PORTLESS for item in remaining
    )
    if not shared_dispatcher_still_needed:
        stop_gateway(target)
    click.echo(f"Unpublished {name}.")
