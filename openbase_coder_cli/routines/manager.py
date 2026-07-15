from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Protocol

from super_agents.app_models import LabelQueryInput
from super_agents.app_protocol import (
    extract_thread_id,
    find_turn,
    is_active_status,
    normalize_turn_status,
)
from super_agents.app_time import iso_from_thread_time, iso_now

from .models import (
    DEFAULT_ROUTINE_TIMEZONE,
    JsonObject,
    RoutineRecord,
    get_string,
    parse_routine_command_timeout_seconds,
    routine_active_run_is_stale,
    routine_fresh_thread_name,
    routine_from_patch,
    routine_has_active_run,
    routine_is_due,
    routine_local_date,
    routine_next_run_sort_key,
    routine_next_run_summary,
    routine_turn_input,
    routine_with_next_run,
    without_none,
)
from .store import RoutineState, RoutineStore

logger = logging.getLogger(__name__)


class RoutineExecutionClient(Protocol):
    async def start_thread(self, input_data: JsonObject) -> JsonObject: ...
    async def start_turn(self, input_data: JsonObject) -> JsonObject: ...
    async def read_thread(
        self, thread_id: str, include_turns: bool = True
    ) -> JsonObject: ...
    async def resolve_queue_target(self, input_data: LabelQueryInput) -> Any: ...
    async def start_or_queue_turn(
        self, target: Any, turn_input: JsonObject
    ) -> JsonObject: ...
    async def get_session(self, thread_id: str) -> Any | None: ...


class RoutineManager:
    """Openbase scheduler and persistence adapter over generic agent primitives."""

    def __init__(
        self,
        client: RoutineExecutionClient,
        *,
        store: RoutineStore | None = None,
    ) -> None:
        self.client = client
        self.store = store or RoutineStore()

    async def save_routine(self, input_data: JsonObject) -> JsonObject:
        name = str(input_data["name"])

        def update(state: RoutineState) -> JsonObject:
            now = iso_now()
            current = state.routines.get(name)
            raw = current.to_json() if current else {"name": name, "createdAt": now}
            for key, value in input_data.items():
                if value is not None:
                    raw[key] = value
            raw["name"] = name
            raw["updatedAt"] = now
            raw.setdefault("createdAt", now)
            raw.setdefault("enabled", True)
            raw.setdefault("timezone", DEFAULT_ROUTINE_TIMEZONE)
            state.routines[name] = routine_from_patch(raw)
            return state.routines[name].to_json()

        return self._response(routine=self.store.update(update))

    async def list_routines(self) -> JsonObject:
        await self.reconcile_routine_terminal_statuses()
        routines = sorted(
            self.store.read().routines.values(),
            key=lambda item: item.updated_at,
            reverse=True,
        )
        return self._response(
            count=len(routines),
            routines=[routine_with_next_run(routine) for routine in routines],
        )

    async def read_routine(self, name: str) -> JsonObject:
        await self.reconcile_routine_terminal_statuses()
        routine = self.store.read().routines.get(name)
        if routine is None:
            raise ValueError(f"No Openbase routine found for name {name}.")
        return self._response(routine=routine_with_next_run(routine))

    async def delete_routine(self, name: str) -> JsonObject:
        def update(state: RoutineState) -> JsonObject:
            if name not in state.routines:
                raise ValueError(f"No Openbase routine found for name {name}.")
            return state.routines.pop(name).to_json()

        return {"deleted": True, "routine": self.store.update(update)}

    async def status_summary(self) -> JsonObject:
        await self.reconcile_routine_terminal_statuses()
        state = self.store.read()
        enabled = [routine for routine in state.routines.values() if routine.enabled]
        return self._response(
            count=len(state.routines),
            enabledCount=len(enabled),
            nextRuns=[
                routine_next_run_summary(routine)
                for routine in sorted(enabled, key=routine_next_run_sort_key)[:5]
            ],
        )

    async def run_due_routines(
        self, name: str | None = None, force: bool = False
    ) -> JsonObject:
        candidates = await self.reserve_due_routines(name=name, force=force)
        results = []
        for routine in sorted(candidates, key=lambda item: item.name):
            results.append(await self.run_routine(routine, force=force))
        return self._response(
            count=len(results),
            failureCount=sum(result.get("ran") is False for result in results),
            results=results,
        )

    async def reserve_due_routines(
        self, name: str | None = None, force: bool = False
    ) -> list[RoutineRecord]:
        await self.reconcile_routine_terminal_statuses()

        def update(state: RoutineState) -> list[RoutineRecord]:
            candidates = [
                routine
                for routine in state.routines.values()
                if (not name or routine.name == name)
                and (
                    force
                    or (routine_is_due(routine) and not routine_has_active_run(routine))
                )
            ]
            if name and not candidates and name not in state.routines:
                raise ValueError(f"No Openbase routine found for name {name}.")
            now = iso_now()
            reserved: list[RoutineRecord] = []
            for routine in candidates:
                merged = {
                    **routine.to_json(),
                    "lastRunDate": routine_local_date(routine),
                    "lastRunAt": now
                    if routine.schedule_type == "interval"
                    else routine.last_run_at,
                    "lastStartedAt": now,
                    "lastStatus": "starting",
                    "lastError": None,
                    "updatedAt": now,
                }
                reserved_routine = routine_from_patch(merged)
                state.routines[routine.name] = reserved_routine
                reserved.append(reserved_routine)
            return reserved

        return self.store.update(update)

    async def run_routine(
        self, routine: RoutineRecord, *, force: bool = False
    ) -> JsonObject:
        if not routine.enabled and not force:
            return {"name": routine.name, "skipped": True, "reason": "disabled"}
        run_date = routine_local_date(routine)
        run_at = routine.last_run_at or iso_now()
        try:
            result = (
                await self.run_command_routine(routine)
                if routine.kind == "command"
                else await self.run_agent_routine(routine)
            )
            await asyncio.sleep(0)
            launch_status, launch_error = await self.routine_launch_status(
                routine, result
            )
            await self.record_routine_run(
                routine.name,
                {
                    "lastRunDate": run_date,
                    "lastRunAt": run_at
                    if routine.schedule_type == "interval"
                    else routine.last_run_at,
                    "lastStartedAt": iso_now(),
                    "lastThreadId": get_string(result, "threadId"),
                    "lastTurnId": get_string(result, "turnId"),
                    "lastStatus": launch_status,
                    "lastError": launch_error,
                },
            )
            return {
                "name": routine.name,
                "ran": launch_status != "failed",
                **({"error": launch_error} if launch_error else {}),
                **result,
            }
        except Exception as exc:
            await self.record_routine_run(
                routine.name,
                {
                    "lastRunDate": run_date,
                    "lastRunAt": run_at
                    if routine.schedule_type == "interval"
                    else routine.last_run_at,
                    "lastStartedAt": iso_now(),
                    "lastStatus": "failed",
                    "lastError": str(exc),
                },
            )
            logger.exception("Failed to run Openbase routine name=%s", routine.name)
            return {"name": routine.name, "ran": False, "error": str(exc)}

    async def run_agent_routine(self, routine: RoutineRecord) -> JsonObject:
        if routine.fresh_thread_per_run:
            name = routine_fresh_thread_name(routine)
            agent_name = await self.routine_fresh_thread_agent_name(routine)
            return await self.start_new_routine_thread(
                routine, name=name, agent_name=agent_name
            )
        if routine.thread_id:
            target = await self.client.resolve_queue_target(
                LabelQueryInput(thread_id=routine.thread_id, cwd=routine.cwd)
            )
            return await self.client.start_or_queue_turn(
                target, routine_turn_input(routine)
            )
        if routine.target_name:
            target = await self.client.resolve_queue_target(
                LabelQueryInput(
                    label=routine.target_name, cwd=routine.cwd, prefer="latest_any"
                )
            )
            try:
                return await self.client.start_or_queue_turn(
                    target, routine_turn_input(routine)
                )
            except RuntimeError as exc:
                if not _is_thread_not_found_error(exc):
                    raise
                logger.warning(
                    "Routine target thread no longer exists; creating a replacement name=%s thread_id=%s",
                    routine.name,
                    target.session.thread_id,
                )
                return await self.start_new_routine_thread(
                    routine,
                    name=routine.target_name,
                    agent_name=target.session.agent_name,
                )
        return await self.start_new_routine_thread(routine, name=routine.name)

    async def start_new_routine_thread(
        self,
        routine: RoutineRecord,
        *,
        name: str,
        agent_name: str | None = None,
    ) -> JsonObject:
        thread_result = await self.client.start_thread(
            without_none(
                {
                    "name": name,
                    "agentName": agent_name,
                    "cwd": routine.cwd,
                    "approvalPolicy": routine.approval_policy,
                    "sandboxType": routine.sandbox_type,
                    "developerInstructions": routine.developer_instructions,
                }
            )
        )
        thread_id = extract_thread_id(thread_result)
        if not thread_id:
            raise RuntimeError(f"Could not start thread for routine {routine.name}.")
        return await self.client.start_turn(
            {
                **routine_turn_input(routine),
                "threadId": thread_id,
                "name": name,
                "label": name,
            }
        )

    async def run_command_routine(self, routine: RoutineRecord) -> JsonObject:
        if not routine.command:
            raise RuntimeError(f"Command routine {routine.name} is missing a command.")
        timeout = parse_routine_command_timeout_seconds(routine.command_timeout_seconds)
        process = await asyncio.create_subprocess_shell(
            routine.command,
            cwd=routine.cwd or None,
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise RuntimeError(
                f"Command routine {routine.name} timed out after {timeout} seconds."
            ) from exc
        return {
            "kind": "command",
            "command": routine.command,
            "exitCode": process.returncode,
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        }

    async def routine_fresh_thread_agent_name(
        self, routine: RoutineRecord
    ) -> str | None:
        if not routine.target_name:
            return None
        try:
            target = await self.client.resolve_queue_target(
                LabelQueryInput(
                    label=routine.target_name, cwd=routine.cwd, prefer="latest_any"
                )
            )
        except ValueError:
            return None
        return target.session.agent_name

    async def routine_launch_status(
        self,
        routine: RoutineRecord,
        result: JsonObject,
    ) -> tuple[str, str | None]:
        if routine.kind == "command":
            exit_code = result.get("exitCode")
            return (
                ("completed", None)
                if exit_code == 0
                else ("failed", f"Command routine exited with code {exit_code}.")
            )
        if result.get("queued"):
            return "queued", None
        thread_id = get_string(result, "threadId")
        turn_id = get_string(result, "turnId")
        if not thread_id or not turn_id:
            return "failed", "Routine turn launch did not return a threadId and turnId."
        session = await self.client.get_session(thread_id)
        turn = (session.turns or {}).get(turn_id) if session is not None else None
        if turn is not None and not is_active_status(turn.status):
            return (
                "failed",
                f"Routine turn became {turn.status} immediately after launch.",
            )
        return "started", None

    async def reconcile_routine_terminal_statuses(self) -> None:
        observed: dict[str, tuple[str | None, str | None, str | None, str | None]] = {}
        for routine in self.store.read().routines.values():
            if not routine_has_active_run(routine):
                continue
            status = None
            last_update_at = routine.last_started_at or routine.updated_at
            session = None
            if routine.last_thread_id:
                try:
                    thread_payload = await self.client.read_thread(
                        routine.last_thread_id,
                        include_turns=True,
                    )
                except (RuntimeError, ValueError):
                    logger.warning(
                        "Could not refresh routine turn status name=%s thread_id=%s",
                        routine.name,
                        routine.last_thread_id,
                        exc_info=True,
                    )
                else:
                    turn = find_turn(thread_payload, routine.last_turn_id or "")
                    if turn is not None:
                        status = normalize_turn_status(turn)
                    raw_thread = thread_payload.get("thread")
                    if isinstance(raw_thread, dict):
                        last_update_at = iso_from_thread_time(raw_thread)
                if status is None:
                    session = await self.client.get_session(routine.last_thread_id)
            if status is None and session is not None:
                turn = (session.turns or {}).get(routine.last_turn_id or "")
                if turn is not None:
                    status = turn.status
                    last_update_at = turn.updated_at
                else:
                    last_update_at = session.last_event_at or session.updated_at
                if status is None and session.last_turn_id == routine.last_turn_id:
                    status = session.last_status
            observed[routine.name] = (
                routine.last_thread_id,
                routine.last_turn_id,
                status,
                last_update_at,
            )

        def update(state: RoutineState) -> None:
            now = iso_now()
            for name, (thread_id, turn_id, status, last_update_at) in observed.items():
                routine = state.routines.get(name)
                if (
                    routine is None
                    or not routine_has_active_run(routine)
                    or routine.last_thread_id != thread_id
                    or routine.last_turn_id != turn_id
                ):
                    continue
                terminal = status in {"completed", "failed", "cancelled"}
                if not terminal and not routine_active_run_is_stale(
                    routine, last_update_at
                ):
                    continue
                patch: JsonObject = {
                    **routine.to_json(),
                    "lastStatus": "completed" if status == "completed" else "failed",
                    "updatedAt": now,
                }
                if status in {"failed", "cancelled"} and not routine.last_error:
                    patch["lastError"] = f"Routine turn became {status}."
                elif not terminal:
                    patch["lastError"] = (
                        "Routine run stopped reporting progress and was released as stale."
                    )
                state.routines[name] = routine_from_patch(patch)

        if observed:
            self.store.update(update)

    async def record_routine_run(self, name: str, patch: JsonObject) -> None:
        def update(state: RoutineState) -> None:
            routine = state.routines.get(name)
            if routine is None:
                return
            state.routines[name] = routine_from_patch(
                {**routine.to_json(), **patch, "updatedAt": iso_now()}
            )

        self.store.update(update)

    @staticmethod
    def _response(**payload: Any) -> JsonObject:
        return {
            **payload,
            "nativeSupport": False,
            "scheduler": "openbase-coder-local",
        }


def _is_thread_not_found_error(exc: RuntimeError) -> bool:
    try:
        payload = json.loads(str(exc))
    except (json.JSONDecodeError, TypeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("code") == -32600
        and str(payload.get("message", "")).startswith("thread not found:")
    )
