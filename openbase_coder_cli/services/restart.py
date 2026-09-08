from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any

import click

from openbase_coder_cli.services.definitions import SERVICES
from openbase_coder_cli.services.launchd import (
    install_service,
    launchctl_bootout,
    launchctl_status,
)
from openbase_coder_cli.services.registry import find_service, require_installation
from openbase_coder_cli.services.selection import configured_default_services
from openbase_coder_cli.services.voice_warning import (
    any_service_action_interrupts_voice,
    warn_before_voice_interruption,
)

DEFAULT_RESTART_DELAY_SECONDS = 8.0
API_RESTART_DELAY_SECONDS = 0.5


@dataclass(frozen=True)
class RestartRequest:
    services: tuple[str, ...] = ()
    recreate_dispatcher: bool = False
    delay_seconds: float = DEFAULT_RESTART_DELAY_SECONDS


@dataclass(frozen=True)
class RestartPlan:
    services: tuple[str, ...]
    recreate_dispatcher: bool
    interrupts_voice: bool
    delay_seconds: float


def restart_target_names() -> list[str]:
    return [service.name for service in SERVICES]


def build_restart_plan(request: RestartRequest) -> RestartPlan:
    service_names = [service.name for service in SERVICES]
    default_service_names = [service.name for service in configured_default_services()]
    valid_targets = set(service_names)

    requested_targets = list(request.services) or default_service_names
    unknown = [target for target in requested_targets if target not in valid_targets]
    if unknown:
        valid = ", ".join(restart_target_names())
        raise click.ClickException(
            f"Unknown restart target '{unknown[0]}'. Valid: {valid}"
        )

    services = _expand_restart_targets(requested_targets)

    if request.recreate_dispatcher:
        _append_unique(services, "livekit-agent")

    service_defs = [find_service(name) for name in services]
    return RestartPlan(
        services=tuple(services),
        recreate_dispatcher=request.recreate_dispatcher,
        interrupts_voice=any_service_action_interrupts_voice(service_defs, "restart"),
        delay_seconds=max(request.delay_seconds, 0.0),
    )


def schedule_restart(
    request: RestartRequest,
    *,
    warn: bool = True,
    emit_cli_warning: bool = True,
) -> RestartPlan:
    require_installation()

    plan = build_restart_plan(request)
    if warn and plan.interrupts_voice:
        warn_before_voice_interruption(
            reason="restart",
            emit_cli_warning=emit_cli_warning,
        )

    command = _scheduled_restart_command(plan)
    subprocess.Popen(
        ["/bin/sh", "-c", command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return plan


def execute_restart_plan(plan: RestartPlan) -> None:
    config = require_installation()

    if plan.delay_seconds > 0:
        time.sleep(plan.delay_seconds)

    if plan.recreate_dispatcher:
        from openbase_coder_cli.livekit_voice_route import (
            prepare_livekit_dispatcher_recreation,
        )

        prepare_livekit_dispatcher_recreation()

    services = [find_service(name) for name in plan.services]
    # Stop consumers before the services they depend on, then start providers
    # first so consumers reconnect to the new process.
    for service in reversed(services):
        if launchctl_status(service)["installed"]:
            launchctl_bootout(service)

    if services:
        time.sleep(2)

    for service in services:
        install_service(config, service)

    if plan.recreate_dispatcher:
        from openbase_coder_cli.livekit_voice_route import (
            warm_livekit_dispatcher_thread,
        )

        asyncio.run(warm_livekit_dispatcher_thread(fresh=True))


def execute_restart_payload(raw_payload: str) -> None:
    payload = json.loads(raw_payload)
    execute_restart_plan(
        RestartPlan(
            services=tuple(payload.get("services") or ()),
            recreate_dispatcher=bool(payload.get("recreate_dispatcher")),
            interrupts_voice=bool(payload.get("interrupts_voice")),
            delay_seconds=float(payload.get("delay_seconds") or 0.0),
        )
    )


def restart_plan_payload(plan: RestartPlan) -> dict[str, Any]:
    return {
        "services": list(plan.services),
        "recreate_dispatcher": plan.recreate_dispatcher,
        "interrupts_voice": plan.interrupts_voice,
        "delay_seconds": plan.delay_seconds,
    }


def _scheduled_restart_command(plan: RestartPlan) -> str:
    payload = json.dumps(asdict(plan), separators=(",", ":"))
    script = (
        "from openbase_coder_cli.services.restart import execute_restart_payload; "
        f"execute_restart_payload({payload!r})"
    )
    return f"exec {shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _expand_restart_targets(requested_targets: list[str]) -> list[str]:
    definitions = {service.name: service for service in SERVICES}
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise click.ClickException(
                f"Restart dependency cycle detected at '{name}'."
            )
        visiting.add(name)
        _append_unique(ordered, name)
        for dependent in definitions[name].restart_dependents:
            if dependent not in definitions:
                raise click.ClickException(
                    f"Service '{name}' declares unknown restart dependent "
                    f"'{dependent}'."
                )
            visit(dependent)
        visiting.remove(name)
        visited.add(name)

    for target in requested_targets:
        visit(target)
    return ordered
