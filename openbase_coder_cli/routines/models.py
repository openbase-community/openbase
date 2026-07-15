from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from super_agents.app_time import age_ms, iso_now, parse_iso_ms

JsonObject = dict[str, Any]
Mode = Literal["default", "plan"]

DEFAULT_ROUTINE_TIMEZONE = "America/New_York"
DEFAULT_ROUTINE_INTERVAL_SECONDS = 60
MIN_ROUTINE_INTERVAL_SECONDS = 5
ACTIVE_ROUTINE_STATUSES = {"starting", "started", "queued"}
DEFAULT_ROUTINE_ACTIVE_STALE_SECONDS = 6 * 60 * 60
DEFAULT_ROUTINE_COMMAND_TIMEOUT_SECONDS = 300


@dataclass(slots=True)
class RoutineRecord:
    name: str
    prompt: str
    time: str
    updated_at: str
    kind: str = "agent"
    schedule_type: str = "daily"
    interval_seconds: int | None = None
    timezone: str | None = None
    enabled: bool = True
    target_name: str | None = None
    thread_id: str | None = None
    fresh_thread_per_run: bool = False
    cwd: str | None = None
    approval_policy: str | None = None
    sandbox_type: str | None = None
    mode: Mode | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    developer_instructions: str | None = None
    command: str | None = None
    command_timeout_seconds: int | None = None
    created_at: str | None = None
    last_run_date: str | None = None
    last_run_at: str | None = None
    last_started_at: str | None = None
    last_thread_id: str | None = None
    last_turn_id: str | None = None
    last_status: str | None = None
    last_error: str | None = None

    def to_json(self) -> JsonObject:
        return without_none(
            {
                "name": self.name,
                "prompt": self.prompt,
                "time": self.time,
                "kind": self.kind,
                "scheduleType": self.schedule_type,
                "intervalSeconds": self.interval_seconds,
                "timezone": self.timezone,
                "enabled": self.enabled,
                "targetName": self.target_name,
                "threadId": self.thread_id,
                "freshThreadPerRun": self.fresh_thread_per_run,
                "cwd": self.cwd,
                "approvalPolicy": self.approval_policy,
                "sandboxType": self.sandbox_type,
                "mode": self.mode,
                "model": self.model,
                "reasoningEffort": self.reasoning_effort,
                "serviceTier": self.service_tier,
                "developerInstructions": self.developer_instructions,
                "command": self.command,
                "commandTimeoutSeconds": self.command_timeout_seconds,
                "createdAt": self.created_at,
                "updatedAt": self.updated_at,
                "lastRunDate": self.last_run_date,
                "lastRunAt": self.last_run_at,
                "lastStartedAt": self.last_started_at,
                "lastThreadId": self.last_thread_id,
                "lastTurnId": self.last_turn_id,
                "lastStatus": self.last_status,
                "lastError": self.last_error,
            }
        )


def routine_from_patch(value: JsonObject) -> RoutineRecord:
    routine = routine_record_from_json(
        value,
        default_timezone=DEFAULT_ROUTINE_TIMEZONE,
        default_updated_at=iso_now(),
    )
    if routine is None:
        raise ValueError("Invalid routine payload.")
    return routine


def routine_record_from_json(
    value: Any,
    *,
    name_fallback: str = "",
    default_time: str = "09:00",
    default_timezone: str | None = None,
    default_updated_at: str = "1970-01-01T00:00:00.000Z",
    require_prompt_time: bool = False,
) -> RoutineRecord | None:
    if not isinstance(value, dict):
        return None
    name = get_string(value, "name") or name_fallback
    prompt = get_string(value, "prompt") or ""
    kind = as_routine_kind(get_string(value, "kind"))
    time_value = get_string(value, "time") or default_time
    schedule_type = as_routine_schedule_type(get_string(value, "scheduleType"))
    if not name:
        return None
    if require_prompt_time and kind != "command" and not prompt:
        return None
    if (
        require_prompt_time
        and schedule_type != "interval"
        and not get_string(value, "time")
    ):
        return None
    return RoutineRecord(
        name=name,
        prompt=prompt,
        time=time_value,
        kind=kind,
        schedule_type=schedule_type,
        interval_seconds=get_positive_int(value, "intervalSeconds"),
        timezone=get_string(value, "timezone") or default_timezone,
        enabled=value.get("enabled")
        if isinstance(value.get("enabled"), bool)
        else True,
        target_name=get_string(value, "targetName"),
        thread_id=get_string(value, "threadId"),
        fresh_thread_per_run=(
            bool(value.get("freshThreadPerRun"))
            if isinstance(value.get("freshThreadPerRun"), bool)
            else False
        ),
        cwd=get_string(value, "cwd"),
        approval_policy=get_string(value, "approvalPolicy"),
        sandbox_type=get_string(value, "sandboxType"),
        mode=as_mode(get_string(value, "mode")),
        model=get_string(value, "model"),
        reasoning_effort=get_string(value, "reasoningEffort"),
        service_tier=get_string(value, "serviceTier"),
        developer_instructions=get_string(value, "developerInstructions"),
        command=get_string(value, "command"),
        command_timeout_seconds=get_positive_int(value, "commandTimeoutSeconds"),
        created_at=get_string(value, "createdAt"),
        updated_at=get_string(value, "updatedAt") or default_updated_at,
        last_run_date=get_string(value, "lastRunDate"),
        last_run_at=get_string(value, "lastRunAt"),
        last_started_at=get_string(value, "lastStartedAt"),
        last_thread_id=get_string(value, "lastThreadId"),
        last_turn_id=get_string(value, "lastTurnId"),
        last_status=get_string(value, "lastStatus"),
        last_error=get_string(value, "lastError"),
    )


def routine_turn_input(routine: RoutineRecord) -> JsonObject:
    return without_none(
        {
            "prompt": routine.prompt,
            "cwd": routine.cwd,
            "approvalPolicy": routine.approval_policy,
            "sandboxType": routine.sandbox_type,
            "mode": routine.mode or "default",
            "model": routine.model,
            "reasoningEffort": routine.reasoning_effort,
            "serviceTier": routine.service_tier,
            "developerInstructions": routine.developer_instructions,
            "name": routine.target_name or routine.name,
            "label": routine.target_name or routine.name,
        }
    )


def routine_fresh_thread_name(routine: RoutineRecord) -> str:
    timestamp = routine.last_started_at or routine.last_run_at or iso_now()
    normalized = re.sub(r"[^0-9A-Za-z]+", "-", timestamp).strip("-")
    return f"{routine.name}-{normalized}" if normalized else routine.name


def routine_with_next_run(routine: RoutineRecord) -> JsonObject:
    return {**routine.to_json(), "nextRunAt": safe_routine_next_run_at(routine)}


def routine_next_run_summary(routine: RoutineRecord) -> JsonObject:
    return {
        "name": routine.name,
        "kind": routine.kind,
        "time": routine.time,
        "scheduleType": routine.schedule_type,
        "intervalSeconds": routine.interval_seconds,
        "timezone": routine.timezone or DEFAULT_ROUTINE_TIMEZONE,
        "nextRunAt": safe_routine_next_run_at(routine),
        "lastStatus": routine.last_status,
    }


def routine_next_run_sort_key(routine: RoutineRecord) -> int:
    next_run = safe_routine_next_run_at(routine)
    return parse_iso_ms(next_run) if next_run else 0


def routine_is_due(routine: RoutineRecord) -> bool:
    if not routine.enabled:
        return False
    if routine.schedule_type == "interval":
        try:
            interval_seconds = parse_routine_interval_seconds(routine.interval_seconds)
        except ValueError:
            return False
        last_run_ms = parse_iso_ms(routine.last_run_at)
        return (
            last_run_ms <= 0
            or parse_iso_ms(iso_now()) - last_run_ms >= interval_seconds * 1000
        )
    try:
        now = routine_now(routine)
        hour, minute = parse_routine_time(routine.time)
    except ValueError:
        return False
    if routine.last_run_date == now.date().isoformat():
        return False
    return (now.hour, now.minute) >= (hour, minute)


def routine_has_active_run(routine: RoutineRecord) -> bool:
    return routine.last_status in ACTIVE_ROUTINE_STATUSES


def routine_active_run_is_stale(
    routine: RoutineRecord, last_update_at: str | None = None
) -> bool:
    if not routine_has_active_run(routine):
        return False
    age = age_ms(last_update_at or routine.last_started_at or routine.updated_at)
    return bool(age is not None and age > routine_active_stale_seconds() * 1000)


def routine_active_stale_seconds() -> int:
    raw = os.environ.get("OPENBASE_CODER_ROUTINE_ACTIVE_STALE_SECONDS")
    try:
        value = int(raw) if raw else DEFAULT_ROUTINE_ACTIVE_STALE_SECONDS
    except ValueError:
        return DEFAULT_ROUTINE_ACTIVE_STALE_SECONDS
    return max(60, value)


def routine_local_date(routine: RoutineRecord) -> str:
    return routine_now(routine).date().isoformat()


def routine_next_run_at(routine: RoutineRecord) -> str:
    if routine.schedule_type == "interval":
        return routine_interval_next_run_at(routine)
    now = routine_now(routine)
    hour, minute = parse_routine_time(routine.time)
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if routine.last_run_date == now.date().isoformat() or next_run <= now:
        next_run += timedelta(days=1)
    return (
        next_run.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def safe_routine_next_run_at(routine: RoutineRecord) -> str | None:
    try:
        return routine_next_run_at(routine)
    except ValueError:
        return None


def routine_now(routine: RoutineRecord) -> datetime:
    try:
        return datetime.now(ZoneInfo(routine.timezone or DEFAULT_ROUTINE_TIMEZONE))
    except ZoneInfoNotFoundError:
        return datetime.now(ZoneInfo(DEFAULT_ROUTINE_TIMEZONE))


def parse_routine_time(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("routine time must use HH:MM.")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("routine time must use HH:MM in 24-hour time.")
    return hour, minute


def parse_routine_interval_seconds(value: int | None) -> int:
    interval = value or DEFAULT_ROUTINE_INTERVAL_SECONDS
    if interval < MIN_ROUTINE_INTERVAL_SECONDS:
        raise ValueError("routine interval must be at least 5 seconds.")
    return interval


def routine_interval_next_run_at(routine: RoutineRecord) -> str:
    interval_seconds = parse_routine_interval_seconds(routine.interval_seconds)
    now_ms = parse_iso_ms(iso_now())
    last_run_ms = parse_iso_ms(routine.last_run_at)
    next_run_ms = (
        now_ms
        if last_run_ms <= 0
        else max(now_ms, last_run_ms + interval_seconds * 1000)
    )
    return (
        datetime.fromtimestamp(next_run_ms / 1000, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_routine_command_timeout_seconds(value: int | None) -> int:
    timeout = value or DEFAULT_ROUTINE_COMMAND_TIMEOUT_SECONDS
    if timeout < 1:
        raise ValueError("routine command timeout must be at least 1 second.")
    return timeout


def get_string(value: JsonObject, key: str) -> str | None:
    result = value.get(key)
    return result if isinstance(result, str) else None


def get_positive_int(value: JsonObject, key: str) -> int | None:
    result = value.get(key)
    return (
        result
        if isinstance(result, int) and not isinstance(result, bool) and result > 0
        else None
    )


def as_routine_schedule_type(value: str | None) -> str:
    return value if value in {"daily", "interval"} else "daily"


def as_routine_kind(value: str | None) -> str:
    return value if value in {"agent", "command"} else "agent"


def as_mode(mode: str | None) -> Mode | None:
    return mode if mode in {"default", "plan"} else None  # type: ignore[return-value]


def without_none(value: JsonObject) -> JsonObject:
    return {key: item for key, item in value.items() if item is not None}
