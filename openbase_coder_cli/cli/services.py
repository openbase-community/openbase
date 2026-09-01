from __future__ import annotations

import subprocess

import click

from openbase_coder_cli.codex_control_plane import codex_app_server_ready
from openbase_coder_cli.paths import DEFAULT_LOG_DIR
from openbase_coder_cli.services.definitions import SERVICES, ServiceDefinition
from openbase_coder_cli.services.launchd import (
    install_all_services,
    install_service,
    launchctl_bootout,
    launchctl_status,
    regenerate_all_services,
    remove_service,
)
from openbase_coder_cli.services.registry import (
    find_service,
    require_installation,
    target_services,
)
from openbase_coder_cli.services.selection import configured_coding_backends
from openbase_coder_cli.services.tailscale_serve import (
    configure_tailscale_serve,
    tailscale_serve_health,
)
from openbase_coder_cli.services.voice_warning import (
    any_service_action_interrupts_voice,
    warn_before_voice_interruption,
)


def _ensure_started(config, svc: ServiceDefinition, verb: str) -> None:
    try:
        install_service(config, svc)
        click.echo(f"  Installed and {verb} {svc.name}")
    except click.ClickException as exc:
        click.echo(f"  Failed to {verb} {svc.name}: {exc}")


def _ensure_tailscale_serve_routes() -> None:
    click.echo()
    click.echo("Configuring Tailscale Serve routes...")
    try:
        configure_tailscale_serve()
    except Exception as exc:
        click.echo(click.style(f"  WARN  {exc}", fg="yellow"))
        click.echo(
            "  Run these manually after Tailscale is installed and connected:\n"
            "    tailscale serve --bg --http=18080 http://127.0.0.1:7999\n"
            "    tailscale serve --bg --tcp=7880 tcp://127.0.0.1:7880"
        )
    else:
        click.echo("  Configured :18080 -> http://127.0.0.1:7999")
        click.echo("  Configured tcp :7880 -> tcp://127.0.0.1:7880")


def _install_default_services() -> None:
    config = require_installation()
    click.echo("Installing launchd services...")
    install_all_services(config)
    _ensure_tailscale_serve_routes()


@click.group()
def services() -> None:
    """Service lifecycle management for Openbase Coder."""


@services.command()
def install() -> None:
    """Generate and load all launchd services."""
    _install_default_services()


@services.command("up")
def up() -> None:
    """Install/start default services and configure Tailscale Serve."""
    _install_default_services()


@services.command()
@click.argument("name", required=False)
def start(name: str | None) -> None:
    """Start all or one service."""
    config = require_installation()
    targets = target_services(name)
    for svc in targets:
        _ensure_started(config, svc, "started")
    if name is None:
        _ensure_tailscale_serve_routes()


@services.command()
@click.argument("name", required=False)
def stop(name: str | None) -> None:
    """Stop all or one service and keep it stopped (unload launchd job)."""
    require_installation()
    targets = target_services(name)
    if any_service_action_interrupts_voice(targets, "stop"):
        warn_before_voice_interruption(reason="services stop")
    for svc in targets:
        if launchctl_bootout(svc):
            click.echo(f"  Stopped {svc.name}")
        else:
            click.echo(f"  {svc.name} not loaded")


@services.command()
@click.argument("name", required=False)
def uninstall(name: str | None) -> None:
    """Stop services and remove their launchd plists and wrapper scripts.

    Works without an installation so a wiped Openbase home can still be
    cleaned up; sweeps every known service regardless of backend gating.
    """
    targets = [svc for svc in SERVICES if svc.name == name] if name else list(SERVICES)
    if name and not targets:
        raise click.ClickException(f"Unknown service: {name}")
    if any_service_action_interrupts_voice(targets, "stop"):
        warn_before_voice_interruption(reason="services uninstall")
    for svc in targets:
        if remove_service(svc):
            click.echo(f"  Uninstalled {svc.name}")
        else:
            click.echo(f"  {svc.name} not installed")


@services.command()
def status() -> None:
    """Show status of all services."""
    require_installation()
    coding_backends = configured_coding_backends()
    backends_label = "/".join(coding_backends)
    has_failure = False
    click.echo("Service Status:")
    click.echo()
    for svc in SERVICES:
        info = launchctl_status(svc)
        name_col = f"  {svc.name:<20}"
        supports = getattr(svc, "supports_backend", None)
        backend_supported = supports is None or any(
            supports(backend) for backend in coding_backends
        )
        required = getattr(svc, "install_by_default", True) and backend_supported
        if not info["installed"]:
            if required:
                click.echo(f"{name_col} not installed")
                has_failure = True
            elif not backend_supported:
                click.echo(f"{name_col} not used ({backends_label} backend)")
            else:
                click.echo(f"{name_col} optional (not installed)")
        elif info["pid"]:
            if svc.name == "codex-app-server" and not codex_app_server_ready():
                click.echo(
                    f"{name_col} running (pid {info['pid']}), but initialize readiness failed"
                )
                has_failure = True
            else:
                click.echo(f"{name_col} running (pid {info['pid']})")
        else:
            exit_code = info.get("last_exit_code", "unknown")
            if required:
                click.echo(f"{name_col} loaded (not running, last exit: {exit_code})")
                has_failure = True
            else:
                click.echo(f"{name_col} optional (not running, last exit: {exit_code})")

    serve_health = tailscale_serve_health()
    click.echo()
    click.echo("Tailscale Serve:")
    if serve_health.healthy:
        click.echo(f"  {'openbase-api':<20} reachable at {serve_health.openbase_url}")
        click.echo("  livekit-server      tcp :7880 -> 127.0.0.1:7880")
    else:
        has_failure = True
        if not serve_health.tailscale_available:
            click.echo("  tailscale           not found on PATH")
        elif not serve_health.tailscale_running:
            click.echo(
                "  tailscale           "
                f"not running ({serve_health.error or 'unknown error'})"
            )
        else:
            click.echo(
                f"  tailscale           running for "
                f"{serve_health.host or 'unknown host'}"
            )
            click.echo(
                "  openbase-api        "
                + (
                    f"configured at {serve_health.openbase_url}"
                    if serve_health.openbase_configured
                    else "missing :18080 -> 127.0.0.1:7999"
                )
            )
            click.echo(
                "  livekit-server      "
                + (
                    "configured tcp :7880 -> 127.0.0.1:7880"
                    if serve_health.livekit_configured
                    else "missing tcp :7880 -> 127.0.0.1:7880"
                )
            )
            click.echo(
                "  external-health     "
                + (
                    "passed"
                    if serve_health.openbase_reachable
                    else f"failed ({serve_health.error or 'unknown error'})"
                )
            )
    click.echo()
    if has_failure:
        raise click.ClickException("One or more Openbase services are unhealthy.")


@services.command()
@click.argument("name")
def logs(name: str) -> None:
    """Tail logs for a service."""
    require_installation()
    svc = find_service(name)
    combined_log = DEFAULT_LOG_DIR / f"{svc.name}.log"
    legacy_stdout_log = DEFAULT_LOG_DIR / f"{svc.name}.stdout.log"
    legacy_stderr_log = DEFAULT_LOG_DIR / f"{svc.name}.stderr.log"

    if not any(
        path.exists() for path in (combined_log, legacy_stdout_log, legacy_stderr_log)
    ):
        raise click.ClickException(f"No log files found for {svc.name}")

    click.echo(f"Tailing logs for {svc.name}... (Ctrl+C to stop)")
    click.echo()

    files = [
        str(f)
        for f in [combined_log, legacy_stdout_log, legacy_stderr_log]
        if f.exists()
    ]
    subprocess.run(["tail", "-f", *files])


@services.command()
def regenerate() -> None:
    """Re-read installation.json, regenerate all plists/wrappers."""
    config = require_installation()
    click.echo("Regenerating wrappers and plists...")
    regenerate_all_services(config)
