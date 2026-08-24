from __future__ import annotations

from dataclasses import dataclass

from openbase_coder_cli.backend_config import (
    CODEX_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)


@dataclass
class ServiceDefinition:
    name: str
    description: str
    # A runner key (see services/runners.py), invoked identically on every
    # platform via ``python -m openbase_coder_cli.services.runners <name>``.
    # Previously held a bash body with OS-branchy network-discovery logic.
    command_template: str
    workdir_template: str
    install_by_default: bool = True
    port: int | None = None
    cleanup_ports: tuple[int, ...] = ()
    cleanup_command_substrings: tuple[str, ...] = ()
    # Coding backends this service applies to; None means all backends.
    backends: tuple[str, ...] | None = None
    service_type: str = "simple"
    restart_policy: str | None = "always"
    keep_alive: bool = True
    # Services that must restart after this service to reconnect cleanly.
    restart_dependents: tuple[str, ...] = ()

    def supports_backend(self, coding_backend: str) -> bool:
        return self.backends is None or coding_backend in self.backends


SERVICES: list[ServiceDefinition] = [
    ServiceDefinition(
        name="livekit-server",
        description="LiveKit Server",
        command_template="livekit-server",
        workdir_template="{workspace}",
        port=7880,
        cleanup_ports=(7880, 7881),
        cleanup_command_substrings=("livekit-server",),
        restart_dependents=("livekit-agent",),
    ),
    ServiceDefinition(
        name="codex-app-server",
        description="Codex App Server",
        command_template="codex-app-server",
        workdir_template="{workspace}",
        port=4500,
        backends=(CODEX_BACKEND, OPENBASE_CLOUD_CODEX_BACKEND),
    ),
    ServiceDefinition(
        name="sync-workers",
        description="Sync Workers (thread, device, and code-sync reconcile)",
        # One process runs every periodic sync job on its own thread — the
        # jobs read their historical interval/max-age env overrides
        # (CODEX_THREAD_SYNC_INTERVAL etc.) from the wrapper-sourced env file
        # themselves, and state-dependent jobs (device sync, reconcile) gate
        # at runtime on code-sync enablement instead of being installed and
        # removed as companion services.
        command_template="sync-workers",
        workdir_template="{data_dir}",
    ),
    ServiceDefinition(
        name="openbase-routines",
        description="Openbase Routines",
        command_template="openbase-routines",
        workdir_template="{data_dir}",
    ),
    ServiceDefinition(
        name="livekit-agent",
        description="LiveKit Agent Worker",
        command_template="livekit-agent",
        workdir_template="{runtime_workdir}",
        cleanup_ports=(8081,),
        cleanup_command_substrings=("openbase_coder_cli.livekit_agent.livekit",),
    ),
    ServiceDefinition(
        name="django-cli",
        description="Django CLI Server",
        command_template="django-cli",
        workdir_template="{data_dir}",
        port=7999,
    ),
    ServiceDefinition(
        name="code-sync",
        description="Code Sync (managed Syncthing)",
        command_template="code-sync",
        workdir_template="{data_dir}",
        # Installed only when code sync is enabled (openbase-coder sync
        # enable or the sync settings API); never on plain installs.
        install_by_default=False,
        cleanup_command_substrings=("syncthing",),
    ),
    ServiceDefinition(
        name="openbase-cloud-auth-rehydrate",
        description="Openbase Cloud workspace auth rehydrate",
        command_template="openbase-cloud-auth-rehydrate",
        workdir_template="{data_dir}",
        install_by_default=False,
        service_type="oneshot",
        restart_policy="on-failure",
        keep_alive=False,
    ),
    ServiceDefinition(
        name="openbase-cloud-heartbeat",
        description="Openbase Cloud idle heartbeat",
        command_template="openbase-cloud-heartbeat",
        workdir_template="{data_dir}",
        # Only meaningful on openbase-cloud workspaces; installed explicitly by
        # `openbase-coder provision`, never on normal local installs.
        install_by_default=False,
    ),
    ServiceDefinition(
        name="openbase-tunneld",
        description="Openbase Tunneld (embedded tailnet, no VPN)",
        command_template="openbase-tunneld",
        workdir_template="{data_dir}",
        # Installed by `openbase-coder tailnet set-provider netmesh-tsnet`
        # (the embedded transport), never on tailscale/VPN installs.
        install_by_default=False,
        port=7998,
        cleanup_ports=(7998,),
        cleanup_command_substrings=("openbase-tunneld",),
    ),
]

TUNNELD_SERVICE = next(s for s in SERVICES if s.name == "openbase-tunneld")


def default_services(coding_backend: str | None = None) -> list[ServiceDefinition]:
    """Services installed by default, optionally filtered to a coding backend."""
    services = [service for service in SERVICES if service.install_by_default]
    if coding_backend is None:
        return services
    return [service for service in services if service.supports_backend(coding_backend)]


# Services that no longer exist; installs remove any leftover units/plists so
# upgrades don't strand old processes running retired commands. Their periodic
# jobs now run inside the consolidated ``sync-workers`` service.
RETIRED_SERVICE_NAMES: tuple[str, ...] = (
    "codex-thread-sync",
    "claude-thread-sync",
    "codex-thread-device-sync",
    "claude-thread-device-sync",
)


def retired_service_stub(name: str) -> ServiceDefinition:
    """A minimal definition for a retired service, for unload/removal only."""
    return ServiceDefinition(
        name=name,
        description=f"Retired service {name}",
        command_template="",
        workdir_template="{data_dir}",
        install_by_default=False,
    )
