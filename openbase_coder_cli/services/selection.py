"""Backend-aware service selection for the current installation."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path

from openbase_coder_cli.env_file import selected_backend_from_env_file
from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH
from openbase_coder_cli.services.definitions import (
    SERVICES,
    ServiceDefinition,
    default_services,
)
from openbase_coder_cli.services.installation import InstallationConfig


def include_installed_optional_services(
    services: Iterable[ServiceDefinition],
    status: Callable[[ServiceDefinition], dict],
) -> list[ServiceDefinition]:
    """Refresh enabled optional daemons without enabling or replaying features."""
    selected = list(services)
    for service in SERVICES:
        if (
            not service.install_by_default
            and service.service_type != "oneshot"
            and service not in selected
            and status(service).get("installed")
        ):
            selected.append(service)
    return selected


def configured_env_file_path() -> Path:
    """The env file recorded by the installation, or the default location."""
    if InstallationConfig.exists():
        try:
            config = InstallationConfig.load()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return DEFAULT_ENV_FILE_PATH
        if config.env_file:
            return Path(config.env_file).expanduser()
    return DEFAULT_ENV_FILE_PATH


def configured_coding_backend() -> str:
    """The coding backend selected by the current installation's env file."""
    return selected_backend_from_env_file(configured_env_file_path())


def configured_default_services() -> list[ServiceDefinition]:
    """Default services applicable to any configured coding backend."""
    backends = configured_coding_backends()
    if len(backends) == 1:
        return default_services(backends[0])
    merged: list[ServiceDefinition] = []
    for backend in backends:
        for service in default_services(backend):
            if service not in merged:
                merged.append(service)
    return merged


def configured_coding_backends() -> list[str]:
    """All configured coding backends, primary first.

    ``OPENBASE_CODING_BACKENDS`` (comma-separated) opts into mixed-backend
    mode; unset, the single selected backend rules as before.
    """
    import os

    from openbase_coder_cli.backend_config import (
        CODING_BACKENDS_ENV_KEY,
        normalize_backend,
    )

    primary = configured_coding_backend()
    raw = os.environ.get(CODING_BACKENDS_ENV_KEY, "").strip()
    if not raw:
        return [primary]
    backends: list[str] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            backend = normalize_backend(chunk)
        except ValueError:
            continue
        if backend not in backends:
            backends.append(backend)
    if not backends:
        return [primary]
    if primary in backends:
        backends.remove(primary)
    return [primary, *backends]


def service_supports_configured_backends(service: ServiceDefinition) -> bool:
    """Whether a service applies to ANY configured backend (mixed-aware)."""
    return any(
        service.supports_backend(backend) for backend in configured_coding_backends()
    )
