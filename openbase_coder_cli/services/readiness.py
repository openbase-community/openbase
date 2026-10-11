"""Wait for provider endpoints before starting their dependent services."""

from __future__ import annotations

import socket
import time

import click

from openbase_coder_cli.services.definitions import ServiceDefinition


def provider_ready(service: ServiceDefinition) -> bool:
    if service.name in {"codex-app-server", "codex-app-server-dispatcher"}:
        from openbase_coder_cli.codex_control_plane import (
            endpoint_is_shared_codex_daemon,
        )
        from openbase_coder_cli.services.codex_version_skew import (
            installed_codex_version,
            running_codex_app_server_version,
            service_endpoint,
            version_is_older,
        )

        installed = installed_codex_version()
        if installed is None:
            return False
        endpoint = service_endpoint(service.name)
        running = running_codex_app_server_version(endpoint)
        if running is None:
            return False
        # Codex's managed daemon serves dependents whatever its version;
        # Openbase does not restart it, so waiting on it cannot help.
        if endpoint_is_shared_codex_daemon(endpoint):
            return True
        # A server newer than the installed CLI is not the stale
        # pre-restart instance either.
        return not version_is_older(running, installed[1])
    if service.port is not None:
        try:
            with socket.create_connection(("127.0.0.1", service.port), timeout=1):
                return True
        except OSError:
            return False
    return True


def wait_for_provider(service: ServiceDefinition, *, timeout: float = 60) -> None:
    if not service.restart_dependents:
        return
    deadline = time.monotonic() + timeout
    while not provider_ready(service):
        if time.monotonic() >= deadline:
            raise click.ClickException(
                f"Service {service.name} did not become ready; dependent services were not restarted."
            )
        time.sleep(0.25)
