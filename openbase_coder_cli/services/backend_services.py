"""Keep backend-scoped services in step with the selected coding backend.

Switching the coding backend only rewrites the env file. On a cloud
workspace that left the services behind: the entrypoint supervises the
wrappers it found at boot and never the dispatcher app server, so a switch
from Claude Code to Codex made both Codex app servers required while
nothing started them, and every app warned that the Codex app server was
unavailable (2026-10-13). This module starts the selected backend's missing
services and stops unused backend services, which a Claude-only
workspace keeps down to save memory.

Only externally supervised installs (containers) are touched: launchd and
systemd installs keep every configured backend's services as before.
"""

from __future__ import annotations

import threading

from openbase_coder_cli.services import launchd
from openbase_coder_cli.services.definitions import SERVICES, ServiceDefinition
from openbase_coder_cli.services.installation import InstallationConfig
from openbase_coder_cli.services.mutation_lock import service_mutation
from openbase_coder_cli.services.registry import require_installation
from openbase_coder_cli.services.selection import configured_coding_backend


def service_wanted(svc: ServiceDefinition, primary: str) -> bool:
    """Whether the primary backend or Dispatcher needs ``svc``.

    Services not tied to a backend are always wanted.
    """
    if svc.supports_backend(primary):
        return True
    from openbase_coder_cli import dispatcher_config

    # The Dispatcher can use Codex while default worker models use Claude.
    # Its tools also need the shared app server. Cloud currently offers only
    # Claude, so a saved local role choice must not enable Codex there.
    if dispatcher_config.backend_location(primary) == "cloud":
        return False
    model = dispatcher_config.backend_model("dispatcher", backend=primary)
    return bool(model) and svc.supports_backend(
        dispatcher_config.identity_for_model(model, "local")
    )


def restart_plan_includes(svc: ServiceDefinition) -> bool:
    """Whether a full restart should bring ``svc`` up.

    Under an external supervisor a backend-scoped service the primary
    backend does not use stays down; elsewhere every configured service is
    restarted, as before.
    """
    if not launchd._external_supervisor():
        return True
    return service_wanted(svc, configured_coding_backend())


@service_mutation()
def reconcile_backend_services(config: InstallationConfig) -> dict[str, list[str]]:
    """Start the selected engines' stopped services; stop the others.

    Returns the service names started and stopped. A no-op outside an
    external supervisor.
    """
    started: list[str] = []
    stopped: list[str] = []
    if not launchd._external_supervisor():
        return {"started": started, "stopped": stopped}
    primary = configured_coding_backend()
    for svc in SERVICES:
        if svc.backends is None:
            continue
        running = bool(launchd.launchctl_status(svc).get("pid"))
        if service_wanted(svc, primary):
            if not running:
                launchd.install_service(config, svc)
                started.append(svc.name)
        else:
            # Mark even a crashed or not-yet-started service: the entrypoint
            # may already be waiting to relaunch it.
            launchd.launchctl_bootout(svc)
            if running:
                stopped.append(svc.name)
    return {"started": started, "stopped": stopped}


def reconcile_backend_services_in_background() -> threading.Thread | None:
    """Reconcile on a daemon thread, for request handlers and server start.

    Starting a service waits for it to answer (up to a minute), which no
    settings request should block on. Returns the thread, or None when
    there is nothing to do here (not externally supervised).
    """
    if not launchd._external_supervisor():
        return None

    def run() -> None:
        reconcile_backend_services(require_installation())

    thread = threading.Thread(
        target=run, name="reconcile-backend-services", daemon=True
    )
    thread.start()
    return thread
