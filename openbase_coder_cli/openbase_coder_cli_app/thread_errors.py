"""Translate thread-store failures into stable user-facing states."""

from __future__ import annotations

import json

THREAD_VERSION_UNAVAILABLE_CODE = "thread_version_unavailable"
THREAD_VERSION_UNAVAILABLE_MESSAGE = (
    "This thread was created by a newer Codex version and cannot be opened "
    "in this version of Openbase yet. You can return to Threads or try again "
    "after updating Openbase."
)


def is_thread_version_unavailable_error(exc: Exception) -> bool:
    """Return whether Codex rejected a rollout written by another version."""
    message = _error_message(exc).casefold()
    return (
        "failed to read thread" in message
        and "does not start with session metadata" in message
    )


def thread_error_message(exc: Exception) -> str:
    """Return a useful message without exposing local paths or internals."""
    if is_thread_version_unavailable_error(exc):
        return THREAD_VERSION_UNAVAILABLE_MESSAGE
    return _error_message(exc)


def thread_error_code(exc: Exception, *, fallback: str) -> str:
    if is_thread_version_unavailable_error(exc):
        return THREAD_VERSION_UNAVAILABLE_CODE
    return fallback


def _error_message(exc: Exception) -> str:
    raw = str(exc)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"]
    return raw
