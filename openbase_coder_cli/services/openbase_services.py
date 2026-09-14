from __future__ import annotations

import click

from openbase_coder_cli.services.definitions import SERVICES, ServiceDefinition
from openbase_coder_cli.services.launchd import (
    install_service,
    launchctl_bootout,
    launchctl_status,
)
from openbase_coder_cli.services.registry import (
    find_service,
    require_installation,
    service_label,
)
from openbase_coder_cli.services.restart import (
    API_RESTART_DELAY_SECONDS,
    RestartRequest,
    restart_plan_payload,
    schedule_restart,
)
from openbase_coder_cli.services.selection import configured_coding_backend
from openbase_coder_cli.services.voice_warning import (
    service_action_interrupts_voice,
    warn_before_voice_interruption,
)

SERVICE_ACTIONS = {"start", "stop", "restart"}


def list_openbase_services_payload() -> dict:
    coding_backend = configured_coding_backend()
    return {
        "services": [
            _service_payload(service, coding_backend=coding_backend)
            for service in SERVICES
        ]
    }


def run_openbase_service_action(service_name: str, action: str) -> dict:
    if action not in SERVICE_ACTIONS:
        valid = ", ".join(sorted(SERVICE_ACTIONS))
        raise click.ClickException(
            f"Unsupported service action '{action}'. Valid: {valid}"
        )

    service = find_service(service_name)

    if action == "restart":
        plan = schedule_restart(
            RestartRequest(
                services=(service.name,),
                delay_seconds=API_RESTART_DELAY_SECONDS,
            ),
            emit_cli_warning=False,
        )
        return {
            **list_openbase_services_payload(),
            "scheduled": True,
            "restart": restart_plan_payload(plan),
        }

    config = require_installation()
    if service_action_interrupts_voice(service, action):
        warn_before_voice_interruption(
            reason=f"settings service {action} {service.name}",
            emit_cli_warning=False,
        )

    if action == "stop":
        launchctl_bootout(service)
    else:
        install_service(config, service)

    return {**list_openbase_services_payload(), "scheduled": False}


def schedule_openbase_restart_payload(
    *,
    service_name: str | None = None,
    recreate_dispatcher: bool = False,
) -> dict:
    """Schedule a unified Openbase-managed service restart."""
    services = (service_name,) if service_name else ()
    plan = schedule_restart(
        RestartRequest(
            services=services,
            recreate_dispatcher=recreate_dispatcher,
            delay_seconds=API_RESTART_DELAY_SECONDS,
        ),
        emit_cli_warning=False,
    )
    return {
        **list_openbase_services_payload(),
        "scheduled": True,
        "restart": restart_plan_payload(plan),
    }


def _service_payload(service: ServiceDefinition, *, coding_backend: str) -> dict:
    status = launchctl_status(service)
    pid = status.get("pid")
    return {
        "name": service.name,
        "label": service_label(service),
        "description": service.description,
        "port": service.port,
        "installed": bool(status.get("installed")),
        "running": bool(pid),
        "pid": pid,
        "last_exit_code": status.get("last_exit_code"),
        "optional": (
            not service.install_by_default
            or not service.supports_backend(coding_backend)
        ),
    }
