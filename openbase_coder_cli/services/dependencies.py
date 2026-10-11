"""Stable dependency ordering shared by installs and restart batches."""

from __future__ import annotations

import click

from openbase_coder_cli.services.definitions import ServiceDefinition


def order_services(services: list[ServiceDefinition]) -> list[ServiceDefinition]:
    """Order the selected subgraph; do not enable unselected optional services."""
    pending = {service.name: service for service in services}
    parents = {name: set() for name in pending}
    for service in pending.values():
        for dependent in service.restart_dependents:
            if dependent in pending:
                parents[dependent].add(service.name)
    ordered = []
    while pending:
        ready = next((name for name in pending if not parents[name]), None)
        if ready is None:
            raise click.ClickException(
                "Service dependency cycle: " + ", ".join(pending)
            )
        ordered.append(pending.pop(ready))
        for dependencies in parents.values():
            dependencies.discard(ready)
    return ordered
