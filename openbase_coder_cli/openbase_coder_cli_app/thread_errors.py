"""Translate unreadable Codex thread failures into safe API errors."""

from __future__ import annotations

import json

THREAD_DATA_UNAVAILABLE_CODE = "thread_data_unavailable"
THREAD_DATA_UNAVAILABLE_MESSAGE = (
    "This thread's saved data is not compatible with the current Codex version. "
    "Update Openbase Coder and Codex, then try again."
)


def is_thread_data_unavailable_error(exc: Exception) -> bool:
    message = _error_message(exc).casefold()
    return "failed to read thread" in message and (
        "does not start with session metadata" in message
        or "failed to read session metadata" in message
    )


def thread_error_message(exc: Exception) -> str:
    if is_thread_data_unavailable_error(exc):
        return THREAD_DATA_UNAVAILABLE_MESSAGE
    return _error_message(exc)


def thread_error_code(exc: Exception, *, fallback: str) -> str:
    """A stable code for a failed thread operation.

    Beyond the unreadable-thread case, a turn or steer that could not reach
    its backend reports super-agents' failure kind (``backend_unavailable``,
    ``start_timeout``) so clients can retry or hold the request instead of
    parsing prose.
    """
    if is_thread_data_unavailable_error(exc):
        return THREAD_DATA_UNAVAILABLE_CODE
    try:
        from super_agents.claude_failure_kinds import classify_turn_failure
    except ImportError:  # older super-agents without the classifier
        return fallback
    return classify_turn_failure(exc) or fallback


def _error_message(exc: Exception) -> str:
    raw = str(exc)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"]
    return raw
