from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import click
from super_agents.app_server_client import CodexAppServerClient

REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
SANDBOX_TYPES = ("readOnly", "workspaceWrite", "dangerFullAccess")
MODES = ("default", "plan")
SCHEDULE_TYPES = ("daily", "interval")
ROUTINE_KINDS = ("agent", "command")
DEFAULT_INTERVAL_SECONDS = 60


def _json_echo(value: dict[str, Any]) -> None:
    click.echo(json.dumps(value, indent=2, sort_keys=True))


def _run_client(coro):
    async def runner():
        client = CodexAppServerClient()
        try:
            return await coro(client)
        finally:
            await client.close()

    try:
        return asyncio.run(runner())
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from None


def _validate_time(value: str) -> str:
    parts = value.split(":")
    if len(parts) != 2:
        raise click.BadParameter("Use HH:MM in 24-hour time.")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        raise click.BadParameter("Use HH:MM in 24-hour time.") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise click.BadParameter("Use HH:MM in 24-hour time.")
    return f"{hour:02d}:{minute:02d}"


def _validate_interval_seconds(value: int) -> int:
    if value < 5:
        raise click.BadParameter("Use an interval of at least 5 seconds.")
    return value


def _resolved_schedule_type(
    schedule_type: str,
    schedule_time: str | None,
    interval_seconds: int | None,
) -> str:
    if (
        schedule_type == "daily"
        and interval_seconds is not None
        and schedule_time is None
    ):
        return "interval"
    return schedule_type


def _routine_patch(
    *,
    name: str,
    prompt: str | None = None,
    kind: str | None = None,
    command: str | None = None,
    command_timeout_seconds: int | None = None,
    schedule_time: str | None = None,
    schedule_type: str | None = None,
    interval_seconds: int | None = None,
    timezone: str | None = None,
    enabled: bool | None = None,
    target_name: str | None = None,
    thread_id: str | None = None,
    fresh_thread_per_run: bool | None = None,
    cwd: Path | None = None,
    approval_policy: str | None = None,
    sandbox_type: str | None = None,
    mode: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    service_tier: str | None = None,
    developer_instructions: str | None = None,
    include_defaults: bool = False,
) -> dict[str, Any]:
    patch: dict[str, Any] = {"name": name}
    if prompt is not None:
        patch["prompt"] = prompt
    if kind is not None:
        patch["kind"] = kind
    if command is not None:
        patch["command"] = command
    if command_timeout_seconds is not None:
        if command_timeout_seconds < 1:
            raise click.BadParameter("Use a command timeout of at least 1 second.")
        patch["commandTimeoutSeconds"] = command_timeout_seconds
    if schedule_time is not None:
        patch["time"] = _validate_time(schedule_time)
    if schedule_type is not None:
        patch["scheduleType"] = schedule_type
    if interval_seconds is not None:
        patch["intervalSeconds"] = _validate_interval_seconds(interval_seconds)
    if timezone:
        patch["timezone"] = timezone
    if enabled is not None:
        patch["enabled"] = enabled
    if target_name:
        patch["targetName"] = target_name
    if thread_id:
        patch["threadId"] = thread_id
    if fresh_thread_per_run is not None:
        patch["freshThreadPerRun"] = fresh_thread_per_run
    if cwd is not None:
        patch["cwd"] = str(cwd.expanduser().resolve())
    if approval_policy or include_defaults:
        patch["approvalPolicy"] = approval_policy or "never"
    if sandbox_type or include_defaults:
        patch["sandboxType"] = sandbox_type or "dangerFullAccess"
    if mode or include_defaults:
        patch["mode"] = mode or "default"
    if model:
        patch["model"] = model
    if reasoning_effort:
        patch["reasoningEffort"] = reasoning_effort
    if service_tier:
        patch["serviceTier"] = service_tier
    if developer_instructions is not None:
        patch["developerInstructions"] = developer_instructions
    return patch


@click.group()
def routines() -> None:
    """Manage Super Agents routines stored outside the MCP tool surface."""


@routines.command("list")
def list_routines() -> None:
    """List persisted routines."""
    _json_echo(_run_client(lambda client: client.list_routines()))


@routines.command("show")
@click.argument("name")
def show_routine(name: str) -> None:
    """Show one persisted routine."""
    _json_echo(_run_client(lambda client: client.read_routine(name)))


@routines.command("create")
@click.argument("name")
@click.option(
    "--kind", type=click.Choice(ROUTINE_KINDS), default="agent", show_default=True
)
@click.option("--prompt", help="Prompt to send when an agent routine runs.")
@click.option("--command", help="Local shell command to run for a command routine.")
@click.option(
    "--command-timeout-seconds", type=int, help="Command routine timeout in seconds."
)
@click.option("--time", "schedule_time", help="Daily HH:MM local time.")
@click.option(
    "--schedule-type",
    type=click.Choice(SCHEDULE_TYPES),
    default="daily",
    show_default=True,
)
@click.option(
    "--interval-seconds", type=int, help="Interval schedule frequency in seconds."
)
@click.option("--timezone", default="America/New_York", show_default=True)
@click.option("--target-name", help="Existing Super Agents thread name to target.")
@click.option("--thread-id", help="Existing Codex app-server thread id to target.")
@click.option(
    "--fresh-thread-per-run",
    is_flag=True,
    help="Create a new Super Agents thread for each routine run.",
)
@click.option("--cwd", type=click.Path(path_type=Path, file_okay=False))
@click.option("--approval-policy", default="never", show_default=True)
@click.option(
    "--sandbox-type",
    type=click.Choice(SANDBOX_TYPES),
    default="dangerFullAccess",
    show_default=True,
)
@click.option("--mode", type=click.Choice(MODES), default="default", show_default=True)
@click.option("--model")
@click.option("--reasoning-effort", type=click.Choice(REASONING_EFFORTS))
@click.option("--service-tier")
@click.option("--developer-instructions")
@click.option("--disabled", is_flag=True, help="Create the routine disabled.")
def create_routine(
    name: str,
    kind: str,
    prompt: str | None,
    command: str | None,
    command_timeout_seconds: int | None,
    schedule_time: str | None,
    schedule_type: str,
    interval_seconds: int | None,
    timezone: str,
    target_name: str | None,
    thread_id: str | None,
    fresh_thread_per_run: bool,
    cwd: Path | None,
    approval_policy: str,
    sandbox_type: str,
    mode: str,
    model: str | None,
    reasoning_effort: str | None,
    service_tier: str | None,
    developer_instructions: str | None,
    disabled: bool,
) -> None:
    """Create or replace a routine."""
    if kind == "agent" and not prompt:
        raise click.ClickException("Agent routines require --prompt.")
    if kind == "command" and not command:
        raise click.ClickException("Command routines require --command.")
    resolved_schedule_type = _resolved_schedule_type(
        schedule_type,
        schedule_time,
        interval_seconds,
    )
    if resolved_schedule_type == "daily" and schedule_time is None:
        raise click.ClickException("Daily routines require --time.")
    if resolved_schedule_type == "interval" and interval_seconds is None:
        interval_seconds = DEFAULT_INTERVAL_SECONDS
    patch = _routine_patch(
        name=name,
        prompt=prompt or "",
        kind=kind,
        command=command,
        command_timeout_seconds=command_timeout_seconds,
        schedule_time=schedule_time,
        schedule_type=resolved_schedule_type,
        interval_seconds=interval_seconds,
        timezone=timezone,
        enabled=not disabled,
        target_name=target_name,
        thread_id=thread_id,
        fresh_thread_per_run=True if fresh_thread_per_run else None,
        cwd=cwd,
        approval_policy=approval_policy,
        sandbox_type=sandbox_type,
        mode=mode,
        model=model,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        developer_instructions=developer_instructions,
        include_defaults=True,
    )
    _json_echo(_run_client(lambda client: client.save_routine(patch)))


@routines.command("update")
@click.argument("name")
@click.option("--kind", type=click.Choice(ROUTINE_KINDS))
@click.option("--prompt")
@click.option("--command")
@click.option("--command-timeout-seconds", type=int)
@click.option("--time", "schedule_time", help="Daily HH:MM local time.")
@click.option("--schedule-type", type=click.Choice(SCHEDULE_TYPES))
@click.option(
    "--interval-seconds", type=int, help="Interval schedule frequency in seconds."
)
@click.option("--timezone")
@click.option("--enable", "enabled", flag_value=True, default=None)
@click.option("--disable", "enabled", flag_value=False)
@click.option("--target-name")
@click.option("--thread-id")
@click.option(
    "--fresh-thread-per-run", "fresh_thread_per_run", flag_value=True, default=None
)
@click.option("--reuse-target-thread", "fresh_thread_per_run", flag_value=False)
@click.option("--cwd", type=click.Path(path_type=Path, file_okay=False))
@click.option("--approval-policy")
@click.option("--sandbox-type", type=click.Choice(SANDBOX_TYPES))
@click.option("--mode", type=click.Choice(MODES))
@click.option("--model")
@click.option("--reasoning-effort", type=click.Choice(REASONING_EFFORTS))
@click.option("--service-tier")
@click.option("--developer-instructions")
def update_routine(
    name: str,
    kind: str | None,
    prompt: str | None,
    command: str | None,
    command_timeout_seconds: int | None,
    schedule_time: str | None,
    schedule_type: str | None,
    interval_seconds: int | None,
    timezone: str | None,
    enabled: bool | None,
    target_name: str | None,
    thread_id: str | None,
    fresh_thread_per_run: bool | None,
    cwd: Path | None,
    approval_policy: str | None,
    sandbox_type: str | None,
    mode: str | None,
    model: str | None,
    reasoning_effort: str | None,
    service_tier: str | None,
    developer_instructions: str | None,
) -> None:
    """Update fields on a routine."""
    resolved_schedule_type = (
        _resolved_schedule_type(schedule_type, schedule_time, interval_seconds)
        if schedule_type
        else _resolved_schedule_type("daily", schedule_time, interval_seconds)
    )
    patch = _routine_patch(
        name=name,
        prompt=prompt,
        kind=kind,
        command=command,
        command_timeout_seconds=command_timeout_seconds,
        schedule_time=schedule_time,
        schedule_type=resolved_schedule_type
        if schedule_type or interval_seconds is not None
        else None,
        interval_seconds=interval_seconds,
        timezone=timezone,
        enabled=enabled,
        target_name=target_name,
        thread_id=thread_id,
        fresh_thread_per_run=fresh_thread_per_run,
        cwd=cwd,
        approval_policy=approval_policy,
        sandbox_type=sandbox_type,
        mode=mode,
        model=model,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        developer_instructions=developer_instructions,
    )
    _json_echo(_run_client(lambda client: client.save_routine(patch)))


@routines.command("delete")
@click.argument("name")
def delete_routine(name: str) -> None:
    """Delete one routine."""
    _json_echo(_run_client(lambda client: client.delete_routine(name)))


@routines.command("run-due")
@click.option("--name", help="Only run the named routine.")
@click.option(
    "--force", is_flag=True, help="Run the named routine even when it is not due."
)
def run_due_routines(name: str | None, force: bool) -> None:
    """Run routines that are currently due."""
    _json_echo(
        _run_client(lambda client: client.run_due_routines(name=name, force=force))
    )


@routines.command("add-webhook-trigger")
@click.argument("name")
@click.option("--description", help="What this trigger listens for.")
@click.option(
    "--sender-path",
    help="Dot path to the sender identity in the event payload, e.g. sender.id.",
)
@click.option(
    "--allow-sender",
    "allow_senders",
    multiple=True,
    help="Sender identity allowed to trigger runs. Repeatable. Required for agent loops.",
)
@click.option(
    "--filter",
    "filters",
    nargs=3,
    multiple=True,
    metavar="PATH OP VALUE",
    help=(
        "Payload filter, e.g. --filter comment.body startsWith /openbase. "
        "Ops: equals, notEquals, contains, startsWith, endsWith, exists, regex."
    ),
)
@click.option(
    "--hmac-secret", help="Shared secret for provider HMAC signatures (SHA-256)."
)
@click.option(
    "--hmac-header",
    help="Header carrying the HMAC signature (default X-Hub-Signature-256).",
)
@click.option(
    "--cloud",
    is_flag=True,
    help=(
        "Also create an Openbase Cloud relay endpoint so external providers "
        "get a public URL; cloud stores events durably and this machine "
        "delivers them on its next poll."
    ),
)
def add_webhook_trigger(
    name: str,
    description: str | None,
    sender_path: str | None,
    allow_senders: tuple[str, ...],
    filters: tuple[tuple[str, str, str], ...],
    hmac_secret: str | None,
    hmac_header: str | None,
    cloud: bool,
) -> None:
    """Add a webhook trigger to a loop and print its ingest token."""
    trigger_input: dict[str, Any] = {}
    if description:
        trigger_input["description"] = description
    if sender_path:
        trigger_input["senderPath"] = sender_path
    if allow_senders:
        trigger_input["senderAllowlist"] = list(allow_senders)
    if filters:
        trigger_input["filters"] = [
            {"path": path, "op": op, "value": value} for path, op, value in filters
        ]
    if hmac_secret:
        trigger_input["hmacSecret"] = hmac_secret
    if hmac_header:
        trigger_input["hmacHeader"] = hmac_header
    if cloud:
        from openbase_coder_cli.services.cloud_webhook_events import (
            create_relay_endpoint,
        )

        relay = create_relay_endpoint(description=description or f"loop:{name}")
        if not relay.ok or not isinstance(relay.response, dict):
            raise click.ClickException(
                "Could not create the Openbase Cloud relay endpoint: "
                f"{relay.error or 'unsupported by this cloud backend'}"
            )
        trigger_input["relayEndpointId"] = relay.response.get("id")
        trigger_input["relayUrl"] = relay.response.get("url")
    result = _run_client(lambda client: client.add_routine_trigger(name, trigger_input))
    token = (result.get("trigger") or {}).get("token")
    if token:
        result["ingestPath"] = f"/api/hooks/t/{token}/"
    relay_url = (result.get("trigger") or {}).get("relayUrl")
    if relay_url:
        result["providerUrl"] = relay_url
    _json_echo(result)


@routines.command("remove-trigger")
@click.argument("name")
@click.argument("trigger_id")
def remove_trigger(name: str, trigger_id: str) -> None:
    """Remove a trigger from a loop."""
    _json_echo(
        _run_client(lambda client: client.remove_routine_trigger(name, trigger_id))
    )


@routines.command("emit")
@click.argument("name")
@click.option("--data", help="JSON payload for the event.")
@click.option("--event-id", help="Optional explicit event id.")
def emit_event(name: str, data: str | None, event_id: str | None) -> None:
    """Run a loop immediately with a locally emitted event."""
    payload = None
    if data:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"--data must be valid JSON: {exc}") from None
    _json_echo(
        _run_client(lambda client: client.emit_routine_event(name, payload, event_id))
    )


DOCTOR_SWEEP_FRESH_SECONDS = 300
DOCTOR_LOG_TAIL_BYTES = 64 * 1024


def _last_sweep_at(log_path: Path) -> str | None:
    import re

    try:
        with log_path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - DOCTOR_LOG_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    matches = re.findall(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \S+ routine_runner sweep_complete",
        tail,
    )
    return matches[-1] if matches else None


def _doctor_routine_report(routine: dict[str, Any], now: Any) -> dict[str, Any]:
    from datetime import datetime

    warnings: list[str] = []
    name = routine.get("name")
    last_status = routine.get("lastStatus")
    last_started_at = routine.get("lastStartedAt")
    if not routine.get("enabled", True):
        warnings.append("Routine is disabled.")
    if not routine.get("lastRunDate"):
        warnings.append("Routine has never run.")
    if last_status in {"starting", "started", "queued"} and last_started_at:
        try:
            started = datetime.fromisoformat(last_started_at.replace("Z", "+00:00"))
            active_hours = (now - started).total_seconds() / 3600
            if active_hours >= 1:
                warnings.append(
                    f"Run has been {last_status} for {active_hours:.1f} hours."
                )
        except ValueError:
            warnings.append(f"Run is {last_status} with unparseable lastStartedAt.")
    if last_status in {"failed", "stale"} and routine.get("lastError"):
        warnings.append(f"Last run {last_status}: {routine['lastError']}")
    last_run_date = routine.get("lastRunDate")
    if routine.get("enabled", True) and last_run_date:
        try:
            days_since = (
                now.date() - datetime.fromisoformat(last_run_date).date()
            ).days
            if routine.get("scheduleType") == "daily" and days_since > 1:
                warnings.append(f"No run recorded for {days_since} days.")
        except ValueError:
            pass
    return {
        "name": name,
        "enabled": routine.get("enabled", True),
        "scheduleType": routine.get("scheduleType"),
        "lastStatus": last_status,
        "lastRunDate": last_run_date,
        "lastStartedAt": last_started_at,
        "nextRunAt": routine.get("nextRunAt"),
        "warnings": warnings,
    }


@routines.command("doctor")
def doctor_routines() -> None:
    """Diagnose routine health: stuck runs, missed runs, and runner liveness.

    Listing routines also reconciles stuck active statuses (terminal or
    stale), so running doctor self-heals routines that a crashed or
    restarted runner left permanently "started".
    """
    from datetime import datetime
    from datetime import timezone as dt_timezone

    from openbase_coder_cli.paths import DEFAULT_LOG_DIR

    listed = _run_client(lambda client: client.list_routines())
    now = datetime.now(dt_timezone.utc)
    reports = [
        _doctor_routine_report(routine, now) for routine in listed.get("routines", [])
    ]

    runner: dict[str, Any] = {"logPath": str(DEFAULT_LOG_DIR / "openbase-routines.log")}
    last_sweep = _last_sweep_at(DEFAULT_LOG_DIR / "openbase-routines.log")
    runner["lastSweepAt"] = last_sweep
    if last_sweep is None:
        runner["warning"] = (
            "No recent sweep_complete found in the runner log; the "
            "openbase-routines service may not be running on this machine."
        )
    else:
        sweep_age = (
            datetime.now() - datetime.strptime(last_sweep, "%Y-%m-%d %H:%M:%S")
        ).total_seconds()
        runner["sweepAgeSeconds"] = int(sweep_age)
        if sweep_age > DOCTOR_SWEEP_FRESH_SECONDS:
            runner["warning"] = (
                f"Last runner sweep was {int(sweep_age)}s ago; the "
                "openbase-routines service may be stopped or stuck."
            )

    warning_count = sum(len(report["warnings"]) for report in reports)
    if "warning" in runner:
        warning_count += 1
    _json_echo(
        {
            "healthy": warning_count == 0,
            "warningCount": warning_count,
            "runner": runner,
            "routines": reports,
        }
    )


SKILLS_AUTO_LINK_SYNC_SECONDS = 300.0
UPDATE_CHECK_SECONDS = 6 * 3600.0


@routines.command("run-loop")
@click.option("--interval", default=60.0, show_default=True, type=float)
@click.option("--verbose", is_flag=True)
def run_loop(interval: float, verbose: bool) -> None:
    """Poll forever and run due routines."""
    from openbase_coder_cli import skills_autolink
    from openbase_coder_cli.runtime import is_standalone_runtime
    from openbase_coder_cli.self_update import (
        SelfUpdateError,
        auto_update_enabled,
        check_for_update,
        spawn_detached_self_update,
    )

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(asctime)s %(name)s %(message)s",
    )
    logger = logging.getLogger(__name__)
    poll_interval = max(interval, 1.0)
    logger.info("routine_runner service_started interval=%s", poll_interval)
    next_skills_sync = time.monotonic()
    next_update_check = time.monotonic()
    last_auto_update_attempt: str | None = None
    while True:
        started = time.monotonic()
        try:
            result = _run_client(lambda client: client.run_due_routines())
            logger.info(
                "routine_runner sweep_complete count=%s results=%s",
                result.get("count"),
                json.dumps(result.get("results", []), sort_keys=True),
            )
        except click.ClickException:
            logger.exception("routine_runner sweep_failed")

        # Periodically re-link personal skills so new ones reach the
        # Openbase agent homes without a service restart.
        if time.monotonic() >= next_skills_sync:
            next_skills_sync = time.monotonic() + max(
                SKILLS_AUTO_LINK_SYNC_SECONDS, poll_interval
            )
            try:
                summary = skills_autolink.sync_auto_linked_skills()
            except OSError:
                logger.exception("skills_autolink sweep_failed")
            else:
                if summary["enabled"] and (summary["created"] or summary["errors"]):
                    logger.info(
                        "skills_autolink sweep_complete created=%s conflicts=%s errors=%s",
                        summary["created"],
                        summary["conflicts"],
                        summary["errors"],
                    )

        # The code-sync reconcile tick moved to the consolidated sync-workers
        # service (openbase_coder_cli.cli.sync_workers).

        # Periodically refresh the update-check cache (standalone installs)
        # so update_available surfaces in status APIs without manual checks,
        # and auto-apply available updates unless opted out.
        if is_standalone_runtime() and time.monotonic() >= next_update_check:
            next_update_check = time.monotonic() + UPDATE_CHECK_SECONDS
            try:
                check = check_for_update()
            except SelfUpdateError as exc:
                logger.warning("update_check failed: %s", exc)
            else:
                if check.update_available:
                    logger.info(
                        "update_check update_available current=%s latest=%s",
                        check.current_version,
                        check.latest_version,
                    )
                    # Retry a given version only for required updates so a
                    # release that health-check-rolled-back does not churn
                    # service restarts every check cycle.
                    should_apply = auto_update_enabled() and (
                        check.update_required
                        or check.latest_version != last_auto_update_attempt
                    )
                    if should_apply:
                        last_auto_update_attempt = check.latest_version
                        try:
                            spawn_detached_self_update(force=check.update_required)
                        except SelfUpdateError as exc:
                            logger.warning("auto_update spawn failed: %s", exc)
                        else:
                            logger.info(
                                "auto_update spawned target=%s forced=%s",
                                check.latest_version,
                                check.update_required,
                            )

        elapsed = time.monotonic() - started
        time.sleep(max(poll_interval - elapsed, 1.0))
