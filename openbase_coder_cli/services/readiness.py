"""Wait for provider endpoints before starting their dependent services."""

from __future__ import annotations

import socket
import time

import click

from openbase_coder_cli.services.definitions import ServiceDefinition


def provider_ready(service: ServiceDefinition) -> bool:
    if service.name in {"codex-app-server", "codex-app-server-dispatcher"}:
        from openbase_coder_cli.services.codex_version_skew import (
            installed_codex_version,
            running_codex_app_server_version,
            service_endpoint,
        )

        installed = installed_codex_version()
        return (
            installed is not None
            and running_codex_app_server_version(service_endpoint(service.name))
            == installed[1]
        )
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
