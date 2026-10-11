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

from openbase_coder_cli.livekit_install import ensure_pinned_livekit_server
from openbase_coder_cli.services.definitions import SERVICES
from openbase_coder_cli.services.dependencies import order_services
from openbase_coder_cli.services.launchd import install_service, launchctl_status
from openbase_coder_cli.services.mutation_lock import service_mutation
from openbase_coder_cli.services.registry import find_service, require_installation
from openbase_coder_cli.services.selection import (
    configured_default_services,
    include_installed_optional_services,
)
from openbase_coder_cli.services.tunneld import install_tunneld_binary
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
    valid_targets = set(service_names)

    requested_targets = list(request.services)
    if not requested_targets:
        requested_targets = [
            service.name
            for service in include_installed_optional_services(
                configured_default_services(), launchctl_status
            )
        ]
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
    config = require_installation()

    plan = build_restart_plan(request)
    if not config.standalone and "livekit-server" in plan.services:
        # Git updates Python's pin but not the separately installed engine.
        # Prepare it before scheduling so download failures are visible to
        # the caller and leave the currently running services untouched.
        if ensure_pinned_livekit_server() is None:
            raise click.ClickException(
                "Could not prepare the pinned LiveKit engine; restart was not "
                "scheduled. Resolve the download error above and retry."
            )
    if not config.standalone and (
        "openbase-tunneld" in plan.services or "openbase-voice-turn" in plan.services
    ):
        try:
            install_tunneld_binary(config)
        except RuntimeError as exc:
            raise click.ClickException(
                f"Could not prepare Openbase Direct; restart was not scheduled: {exc}"
            ) from exc
    if warn and plan.interrupts_voice:
        warn_before_voice_interruption(
            reason="restart",
            emit_cli_warning=emit_cli_warning,
        )

    command = _scheduled_restart_command(plan)
    # The restart runs detached after this process exits; keep its output (a
    # failed Dispatcher recreation, a service that would not install) in a
    # log instead of discarding it (Gabe's VM, 2026-10-11 07:24Z: a silent
    # no-op recreation).
    log_path = restart_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(
            f"\n[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] restart {restart_plan_payload(plan)}\n"
        )
        log.flush()
        subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return plan


def restart_log_path():
    from openbase_coder_cli.paths import DEFAULT_LOG_DIR

    return DEFAULT_LOG_DIR / "restart.log"


def execute_restart_plan(plan: RestartPlan) -> None:
    if plan.delay_seconds > 0:
        time.sleep(plan.delay_seconds)
    with service_mutation():
        config = require_installation()

        previous_dispatcher_id = None
        if plan.recreate_dispatcher:
            from openbase_coder_cli.livekit_voice_route import (
                prepare_livekit_dispatcher_recreation,
            )

            previous_dispatcher_id = (
                prepare_livekit_dispatcher_recreation() or {}
            ).get("previous_dispatcher_thread_id")

        services = order_services([find_service(name) for name in plan.services])
        # Providers first: installation waits for the old process to exit and
        # the provider endpoint to respond before restarting its consumers.
        # Restarts happen in place
        # (no bootout/bootstrap) so macOS does not re-announce the background
        # items on every restart.
        for service in services:
            install_service(config, service)

        if plan.recreate_dispatcher:
            from openbase_coder_cli.livekit_voice_route import (
                warm_livekit_dispatcher_thread,
            )

            new_dispatcher_id = asyncio.run(warm_livekit_dispatcher_thread(fresh=True))
            if previous_dispatcher_id and new_dispatcher_id == previous_dispatcher_id:
                # Loud, never silent: the restart runs detached, so this lands
                # in the restart log (see _scheduled_restart_command).
                raise RuntimeError(
                    "Dispatcher recreation kept the previous Dispatcher thread "
                    f"{previous_dispatcher_id}; no new Dispatcher was created."
                )
            print(
                f"Dispatcher recreated: {previous_dispatcher_id or '-'} -> {new_dispatcher_id}",
                flush=True,
            )


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
    return [
        service.name
        for service in order_services([definitions[name] for name in ordered])
    ]
