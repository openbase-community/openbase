from __future__ import annotations

import json
import sys
from dataclasses import replace
from functools import wraps

import click

from openbase_coder_cli.services import service_recovery
from openbase_coder_cli.services.published_service_routes import (
    allocate_private_service_hostname,
    apply_route,
    release_private_service_hostname,
    remove_route,
)
from openbase_coder_cli.services.published_services import (
    HOSTNAME_TAILNET_PORT,
    MODE_HOSTNAME,
    PublishedService,
    ServiceRegistry,
    allocate_hostname_proxy,
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
            try:
                recovered = service_recovery.recover()
                if recovered:
                    if (
                        function.__name__ == "unpublish"
                        and kwargs.get("name") == recovered
                    ):
                        click.echo(
                            f"Unpublished {recovered} (recovered interrupted operation)."
                        )
                        return
                    click.echo(
                        "Removed an interrupted publication; retrying the requested command."
                    )
            except (OSError, ValueError, RuntimeError) as exc:
                raise click.ClickException(str(exc)) from exc
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
@_registry_transaction
def publish(
    name: str,
    port: int,
    persist: bool | None,
) -> None:
    """Publish loopback HTTP PORT at a memorable tailnet URL named NAME."""
    try:
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
        proxy_port = allocate_hostname_proxy()
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    persistent = _persistence_choice(persist)
    try:
        allocation = allocate_private_service_hostname(name)
        hostname = allocation.hostname
        node_id = allocation.node_id
        hostname_created = allocation.created
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    service_entry = PublishedService(
        name=name,
        local_port=port,
        tailnet_port=HOSTNAME_TAILNET_PORT,
        proxy_port=proxy_port,
        persistent=persistent,
        mode=MODE_HOSTNAME,
        hostname=hostname,
        node_id=node_id,
    )
    desired_services = [*services, service_entry]
    try:
        # Resolve the display URL before any Serve mutation so a provider status
        # failure cannot strand an applied route without durable registry state.
        url = service_url(service_entry)
    except RuntimeError as exc:
        if hostname_created and node_id:
            try:
                release_private_service_hostname(name, node_id)
            except RuntimeError as cleanup_exc:
                raise click.ClickException(
                    f"{exc} Private hostname cleanup also failed: {cleanup_exc}"
                ) from exc
        raise click.ClickException(str(exc)) from exc
    route_applied = False
    applied_hash = None
    try:
        from openbase_coder_cli.services.service_certificates import ensure_certificate

        service_recovery.begin(
            "publish", registry, service_entry, release_hostname=hostname_created
        )
        click.echo(
            "Preparing device-local HTTPS certificate (first issuance can take a few minutes)…"
        )
        ensure_certificate(service_entry)
        save_registry(
            ServiceRegistry(tuple(desired_services), registry.last_applied_serve_hash)
        )
        if persistent:
            install_launchd_service(service_entry)
        else:
            pid = start_ephemeral_gateway(service_entry)
            service_entry = replace(service_entry, pid=pid)
            service_recovery.begin(
                "publish", registry, service_entry, release_hostname=hostname_created
            )
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
        route_applied = True
        save_registry(ServiceRegistry(tuple(desired_services), applied_hash))
    except Exception as exc:
        compensation_errors = []
        if route_applied:
            try:
                remove_route(
                    service_entry,
                    previous_services=desired_services,
                    desired_services=services,
                    last_applied_hash=applied_hash,
                )
            except Exception as compensation_exc:
                compensation_errors.append(
                    f"Serve compensation failed: {compensation_exc}"
                )
        stop_gateway(service_entry)
        try:
            save_registry(registry)
        except OSError as registry_exc:
            compensation_errors.append(f"Registry restoration failed: {registry_exc}")
        if hostname_created and node_id:
            try:
                release_private_service_hostname(name, node_id)
            except RuntimeError as release_exc:
                compensation_errors.append(
                    f"Private hostname cleanup failed: {release_exc}"
                )
        message = str(exc)
        if compensation_errors:
            message += " " + " ".join(compensation_errors)
        else:
            service_recovery.finish()
        raise click.ClickException(message) from exc

    service_recovery.finish()
    click.echo(f"Published {name}: {url}")
    click.echo(f"  Local target: http://127.0.0.1:{port}")
    click.echo("  Mode: account-private hostname")
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
            url = (
                f"http://{item.hostname}/"
                if item.mode == MODE_HOSTNAME
                else f"tailnet :{item.tailnet_port}/{item.name}/"
            )
        mode = "launchd" if item.persistent else "session"
        health = "ready" if gateway_healthy(item, timeout=0.1) else "gateway stopped"
        click.echo(
            f"{item.name:<20} {url} -> 127.0.0.1:{item.local_port} "
            f"({item.mode}, {mode}, {health})"
        )


@service.command()
@click.argument("name")
@click.option(
    "--json", "as_json", is_flag=True, help="Return structured diagnostic checks."
)
def doctor(name: str, as_json: bool) -> None:
    """Diagnose VPN, DNS, certificates, forwarding, and the local application."""
    from openbase_coder_cli.services.published_services import find_service
    from openbase_coder_cli.services.service_diagnostics import diagnose

    item = find_service(name)
    if item is None:
        raise click.ClickException(f"Service '{name}' is not published.")
    if item.tailnet_port != 443 or item.mode != MODE_HOSTNAME:
        raise click.ClickException(
            "Diagnostics require a current HTTPS publication; upgrade and republish this service."
        )
    result = diagnose(item)
    if as_json:
        click.echo(json.dumps(result))
    else:
        for check in result["checks"]:
            click.echo(
                f"{'OK' if check['ok'] else 'FAIL'} {check['check']}: {check['message']}"
            )
    if not result["ready"]:
        raise click.exceptions.Exit(1)


@service.command("recover")
def recover_command() -> None:
    """Safely remove a publication interrupted by a crash or shutdown."""
    with registry_lock():
        try:
            changed = service_recovery.recover()
        except (OSError, ValueError, RuntimeError) as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(
        "Interrupted publication removed." if changed else "No interrupted publication."
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
    remaining = [item for item in services if item.name != name]
    hostname_released = False
    try:
        service_recovery.begin("unpublish", registry, target)
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    if target.mode == MODE_HOSTNAME:
        if not target.node_id:
            raise click.ClickException(
                "Stored hostname publication is missing its Netmesh node ID."
            )
        try:
            release_private_service_hostname(target.name, target.node_id)
            hostname_released = True
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
    route_applied = False
    applied_hash = None
    try:
        save_registry(
            ServiceRegistry(tuple(remaining), registry.last_applied_serve_hash)
        )
        applied_hash = remove_route(
            target,
            previous_services=services,
            desired_services=remaining,
            last_applied_hash=registry.last_applied_serve_hash,
        )
        route_applied = True
        save_registry(ServiceRegistry(tuple(remaining), applied_hash))
    except Exception as exc:
        compensation_errors = []
        if hostname_released:
            try:
                allocation = allocate_private_service_hostname(target.name)
                if (
                    allocation.hostname != target.hostname
                    or allocation.node_id != target.node_id
                ):
                    compensation_errors.append(
                        "Cloud restored a different hostname allocation."
                    )
            except (RuntimeError, ValueError) as restore_exc:
                compensation_errors.append(
                    f"Private hostname restoration failed: {restore_exc}"
                )
        if route_applied:
            try:
                apply_route(
                    target,
                    previous_services=remaining,
                    desired_services=services,
                    last_applied_hash=applied_hash,
                )
            except Exception as compensation_exc:
                compensation_errors.append(
                    f"Serve compensation failed: {compensation_exc}"
                )
        try:
            save_registry(registry)
        except OSError as registry_exc:
            compensation_errors.append(f"Registry restoration failed: {registry_exc}")
        message = str(exc)
        if compensation_errors:
            message += " " + " ".join(compensation_errors)
        else:
            service_recovery.finish()
        raise click.ClickException(message) from exc
    stop_gateway(target)
    service_recovery.finish()
    click.echo(f"Unpublished {name}.")
