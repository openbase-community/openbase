"""Payload parsing and conversion helpers for the Super Agents thread manager.

These helpers interpret Codex app-server / Super Agents payloads and convert
them into Openbase session models. They hold no manager state; the ones that
need Super Agents data take the client as an argument.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from super_agents.app_server_client import (
    extract_thread_cwd,
    extract_thread_id,
    extract_thread_name,
    extract_turn_id,
    is_permission_request,
)

from .models import ThreadInfo as SessionInfo
from .models import ThreadStatus as SessionStatus
from .models import TurnInfo as RunInfo
from .models import TurnSteerInfo as SteerInfo

logger = logging.getLogger(__name__)

THREAD_HISTORY_LIMIT_ENV = "OPENBASE_CODER_THREAD_HISTORY_LIMIT"
DEFAULT_THREAD_HISTORY_LIMIT = 25

RUNNING_STATUSES = {"active", "inProgress", "in_progress", "running", "pending"}
COMPLETED_STATUSES = {"completed", "success"}
ERROR_STATUSES = {"failed", "error", "cancelled", "canceled", "interrupted"}
THREAD_IDLE_STATUSES = {"notLoaded", "not_loaded", "idle", "unknown"}
WAITING_FLAGS = {"waitingOnUserInput", "waiting_on_user_input"}
WAITING_STATUSES = {"waiting", "waitingOnUserInput", "waiting_on_user_input"}


def _timestamp_to_datetime(value: int | float | str | None) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


def _datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _thread_history_limit(env: dict[str, str] | None = None) -> int:
    raw = (env or os.environ).get(THREAD_HISTORY_LIMIT_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_THREAD_HISTORY_LIMIT
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Ignoring invalid %s=%r; using default limit %s",
            THREAD_HISTORY_LIMIT_ENV,
            raw,
            DEFAULT_THREAD_HISTORY_LIMIT,
        )
        return DEFAULT_THREAD_HISTORY_LIMIT


def _status_type(value: Any) -> str:
    if isinstance(value, dict):
        candidate = value.get("type") or value.get("status")
        return candidate if isinstance(candidate, str) else ""
    return value if isinstance(value, str) else ""


def _is_waiting_status(status: Any) -> bool:
    if _status_type(status) in WAITING_STATUSES:
        return True
    if not isinstance(status, dict):
        return False
    flags = status.get("activeFlags")
    if not isinstance(flags, list):
        return False
    return any(flag in WAITING_FLAGS for flag in flags if isinstance(flag, str))


def _thread_status(status: Any) -> SessionStatus:
    if _is_waiting_status(status):
        return SessionStatus.waiting
    status_type = _status_type(status)
    if status_type in ERROR_STATUSES:
        return SessionStatus.error
    if status_type in RUNNING_STATUSES:
        return SessionStatus.running
    if status_type in COMPLETED_STATUSES:
        return SessionStatus.completed
    if status_type in THREAD_IDLE_STATUSES:
        return SessionStatus.idle
    return SessionStatus.idle


def _turn_status(status: Any, error: Any) -> SessionStatus:
    if error not in (None, ""):
        return SessionStatus.error
    if _is_waiting_status(status):
        return SessionStatus.waiting
    status_type = _status_type(status)
    if status_type in COMPLETED_STATUSES:
        return SessionStatus.completed
    if status_type in RUNNING_STATUSES:
        return SessionStatus.running
    if status_type in ERROR_STATUSES:
        return SessionStatus.error
    return SessionStatus.error


def _turn_sort_key(turn: dict[str, Any]) -> int:
    for key in ("completedAt", "finishedAt", "updatedAt", "startedAt", "createdAt"):
        value = turn.get(key)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value:
            try:
                return int(
                    datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                    * 1000
                )
            except ValueError:
                pass
    return 0


def _user_message_texts(turn: dict[str, Any]) -> list[str]:
    """Extract the text of each userMessage item in the turn, in order.

    The first user message started the turn; any later ones are steering
    input sent while the turn was running.
    """
    texts: list[str] = []
    for item in turn.get("items", []):
        if item.get("type") != "userMessage":
            continue
        text_parts = [
            content.get("text", "").strip()
            for content in item.get("content", [])
            if content.get("type") == "text" and content.get("text", "").strip()
        ]
        if text_parts:
            texts.append("\n\n".join(text_parts))
    return texts


def _extract_agent_output(turn: dict[str, Any]) -> str:
    final_parts: list[str] = []
    fallback_parts: list[str] = []
    for item in turn.get("items", []):
        if item.get("type") != "agentMessage":
            continue
        text = item.get("text", "").strip()
        if not text:
            continue
        fallback_parts.append(text)
        phase = item.get("phase")
        if isinstance(phase, str) and phase.startswith("final"):
            final_parts.append(text)
    return (
        "\n\n".join(final_parts or fallback_parts)
        or _optional_turn_string(
            turn,
            "lastUsefulMessage",
            "lastObservedState",
        )
        or ""
    )


def _undelivered_suffix(delivered_text: str, current_text: str) -> str:
    if not current_text:
        return ""
    if not delivered_text:
        return current_text
    if current_text.startswith(delivered_text):
        return current_text[len(delivered_text) :]
    return current_text


def _optional_thread_string(thread: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = thread.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _optional_turn_string(turn: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = turn.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _request_json(request: Any) -> dict[str, Any]:
    to_json = getattr(request, "to_json", None)
    if callable(to_json):
        value = to_json()
        if isinstance(value, dict):
            return value
    if isinstance(request, dict):
        return request
    return {}


def _approval_request_payload(request: Any) -> dict[str, Any]:
    raw = _request_json(request)
    params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    method = raw.get("method")
    if isinstance(method, str) and not is_permission_request(method):
        raise ValueError(f"Request {raw.get('id')} is not an approval request.")
    return {
        "id": raw.get("id"),
        "method": method,
        "params": params,
        "received_at": raw.get("receivedAt") or raw.get("received_at"),
        "thread_id": params.get("threadId") or params.get("thread_id"),
        "turn_id": params.get("turnId") or params.get("turn_id"),
    }


def _thread_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    thread = result.get("thread") if isinstance(result.get("thread"), dict) else result
    return thread if isinstance(thread, dict) else None


def _normalize_backend_thread_payload(payload: dict[str, Any]) -> dict[str, Any]:
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    normalized = {**session, **payload}
    if "threadId" not in normalized and isinstance(normalized.get("id"), str):
        normalized["threadId"] = normalized["id"]
    return normalized


def _next_cursor(result: dict[str, Any]) -> str | None:
    value = result.get("nextCursor") or result.get("next_cursor")
    return value if isinstance(value, str) and value else None


def _is_backend_thread_payload(thread: dict[str, Any]) -> bool:
    return _optional_thread_string(thread, "backend") is not None


def _run_from_turn(
    turn: dict[str, Any], *, raw_status: SessionStatus | None = None
) -> RunInfo:
    turn_id = extract_turn_id(turn)
    if not turn_id:
        raise ValueError("Turn payload is missing an id")
    started_at = _timestamp_to_datetime(
        turn.get("startedAt") or turn.get("createdAt") or turn.get("updatedAt")
    )
    completed_at_value = turn.get("completedAt") or turn.get("finishedAt")
    completed_at = (
        _timestamp_to_datetime(completed_at_value) if completed_at_value else None
    )
    error = turn.get("error")
    stderr = json.dumps(error) if error else ""
    status = _turn_status(turn.get("status"), error)
    if raw_status == SessionStatus.waiting and status == SessionStatus.running:
        status = SessionStatus.waiting
    user_texts = _user_message_texts(turn)
    message = (
        user_texts[0]
        if user_texts
        else _optional_turn_string(turn, "prompt", "promptPreview") or ""
    )

    return RunInfo(
        run_id=turn_id,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        accumulated_output=_extract_agent_output(turn),
        accumulated_stderr=stderr,
        return_code=0 if status == SessionStatus.completed else -1,
        message=message,
        reasoning_effort=_optional_turn_string(
            turn,
            "reasoningEffort",
            "reasoning_effort",
        ),
        steers=[SteerInfo(text=text) for text in user_texts[1:]],
    )


def _session_sort_key(session: SessionInfo) -> datetime:
    if session.current_run is not None:
        return session.current_run.started_at
    if session.run_history:
        last_run = session.run_history[-1]
        return last_run.completed_at or last_run.started_at
    return session.updated_at


def _session_from_thread(
    thread: dict[str, Any],
    *,
    include_turns: bool,
) -> SessionInfo:
    thread_id = extract_thread_id(thread)
    if not thread_id:
        raise ValueError("Thread payload is missing an id")
    raw_status = _thread_status(thread.get("status"))
    uses_backend_active_turn_id = _is_backend_thread_payload(thread)
    name = extract_thread_name(thread)
    status_warning = _optional_thread_string(
        thread,
        "statusWarning",
        "status_warning",
    )
    session = SessionInfo(
        session_id=thread_id,
        directory=extract_thread_cwd(thread) or "",
        name=name,
        agent_name=_optional_thread_string(thread, "agentName", "agent_name"),
        title=_optional_thread_string(thread, "title", "summary"),
        preview=_optional_thread_string(thread, "preview", "description"),
        is_likely_stale=bool(
            thread.get("isLikelyStale") or thread.get("is_likely_stale")
        ),
        status_warning=status_warning,
        backend=_optional_thread_string(thread, "backend"),
        backend_session_id=_optional_thread_string(
            thread,
            "backendSessionId",
            "backend_session_id",
        ),
        session_type="codex",
        created_at=_timestamp_to_datetime(thread.get("createdAt")),
        updated_at=_timestamp_to_datetime(
            thread.get("updatedAt") or thread.get("createdAt")
        ),
        raw_status=raw_status,
        status_override=raw_status if uses_backend_active_turn_id else None,
    )
    if not include_turns:
        return session

    run_history: list[RunInfo] = []
    current_run: RunInfo | None = None
    active_turn_id = _optional_thread_string(thread, "activeTurnId", "active_turn_id")
    turns = sorted(thread.get("turns", []), key=_turn_sort_key)
    for turn in turns:
        turn_id = extract_turn_id(turn)
        if not isinstance(turn, dict) or not turn_id:
            continue
        run = _run_from_turn(
            {
                **turn,
                "id": turn_id,
            },
            raw_status=raw_status,
        )
        is_active_run = run.status in {SessionStatus.running, SessionStatus.waiting}
        if uses_backend_active_turn_id and is_active_run and turn_id != active_turn_id:
            continue
        if status_warning == "stale_active_turn" and is_active_run:
            run_history.append(run)
            continue
        if is_active_run:
            current_run = run
        else:
            run_history.append(run)

    history_limit = _thread_history_limit()
    if len(run_history) > history_limit:
        run_history = run_history[-history_limit:]

    session.current_run = current_run
    session.run_history = run_history
    if (
        current_run is None
        and run_history
        and raw_status
        in {
            SessionStatus.running,
            SessionStatus.waiting,
        }
    ):
        session.raw_status = run_history[-1].status
    return session


async def _merge_tracked_turn_summaries(client: Any, thread: dict[str, Any]) -> None:
    thread_id = extract_thread_id(thread)
    if not thread_id:
        return
    get_session = getattr(client, "get_session", None)
    if not callable(get_session):
        return
    try:
        tracked_session = await get_session(thread_id)
    except Exception:
        logger.debug(
            "Unable to read Super Agents tracked session for thread %s",
            thread_id,
            exc_info=True,
        )
        return
    tracked_turns = getattr(tracked_session, "turns", None) or {}
    if not isinstance(tracked_turns, dict):
        return

    summaries: list[dict[str, Any]] = []
    for turn_id, summary in tracked_turns.items():
        status = getattr(summary, "status", None)
        started_at = getattr(summary, "started_at", None)
        finished_at = getattr(summary, "finished_at", None)
        if not isinstance(status, str) or not isinstance(started_at, str):
            continue
        prompt_preview = getattr(summary, "prompt_preview", None)
        last_useful_message = getattr(summary, "last_useful_message", None)
        items: list[dict[str, Any]] = []
        if isinstance(prompt_preview, str) and prompt_preview:
            items.append(
                {
                    "type": "userMessage",
                    "content": [{"type": "text", "text": prompt_preview}],
                }
            )
        if isinstance(last_useful_message, str) and last_useful_message:
            items.append(
                {
                    "type": "agentMessage",
                    "phase": "final",
                    "text": last_useful_message,
                }
            )
        summaries.append(
            {
                "id": str(getattr(summary, "turn_id", None) or turn_id),
                "status": status,
                "startedAt": started_at,
                "completedAt": (
                    finished_at
                    if status in {"completed", "failed", "cancelled"}
                    else None
                ),
                "items": items,
                "reasoningEffort": getattr(summary, "reasoning_effort", None),
                "error": None,
            }
        )
    if summaries:
        thread["turns"] = summaries


async def _merge_tracked_turn_details(client: Any, thread: dict[str, Any]) -> None:
    """Fill turn fields the app-server payload may lack from tracked state.

    Reasoning effort is only known to the Super Agents turn tracker, and an
    in-flight turn's userMessage items may not be readable yet, so the tracked
    prompt preview keeps the prompt visible while the agent is mid-response.
    """
    thread_id = extract_thread_id(thread)
    if not thread_id:
        return
    get_session = getattr(client, "get_session", None)
    if not callable(get_session):
        return
    try:
        tracked_session = await get_session(thread_id)
    except Exception:
        logger.debug(
            "Unable to read Super Agents tracked session for thread %s",
            thread_id,
            exc_info=True,
        )
        return
    tracked_turns = getattr(tracked_session, "turns", None) or {}
    if not isinstance(tracked_turns, dict):
        return
    for turn in thread.get("turns", []):
        if not isinstance(turn, dict):
            continue
        turn_id = turn.get("id")
        if not isinstance(turn_id, str):
            continue
        summary = tracked_turns.get(turn_id)
        reasoning_effort = getattr(summary, "reasoning_effort", None)
        if isinstance(reasoning_effort, str) and reasoning_effort:
            turn["reasoningEffort"] = reasoning_effort
        prompt_preview = getattr(summary, "prompt_preview", None)
        if (
            isinstance(prompt_preview, str)
            and prompt_preview
            and not _user_message_texts(turn)
            and not _optional_turn_string(turn, "prompt", "promptPreview")
        ):
            turn["promptPreview"] = prompt_preview
