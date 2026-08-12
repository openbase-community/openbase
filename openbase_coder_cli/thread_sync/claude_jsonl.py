"""Low-level Claude Code JSONL transcript parsing and small text/time utils."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .claude_models import CLAUDE_EVENT_TYPES
from .thread_import import _string


def _parse_claude_jsonl(root: Path, session_id: str) -> dict[str, Any] | None:
    seen_event = False
    seen_matching_session = False
    first_user: str | None = None
    latest_assistant: str | None = None
    first_timestamp_ms: int | None = None
    latest_timestamp_ms: int | None = None
    cwd: str | None = None

    try:
        lines = root.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        event_type = _string(payload.get("type"))
        if event_type in CLAUDE_EVENT_TYPES:
            seen_event = True
        payload_session_id = _string(payload.get("sessionId"))
        if payload_session_id == session_id:
            seen_matching_session = True
        cwd = cwd or _string(payload.get("cwd"))
        if timestamp_ms := _timestamp_ms(_string(payload.get("timestamp"))):
            first_timestamp_ms = min(first_timestamp_ms or timestamp_ms, timestamp_ms)
            latest_timestamp_ms = max(latest_timestamp_ms or 0, timestamp_ms)
        role = (
            _string((payload.get("message") or {}).get("role"))
            if isinstance(payload.get("message"), dict)
            else None
        )
        text = _message_text(payload.get("message"))
        if role == "user" and text and first_user is None:
            # Claude Code transcripts often open with harness-injected markup
            # (<local-command-caveat>, <command-name>, <system-reminder>);
            # skip it so session names come from real user text.
            first_user = _meaningful_user_text(text)
        elif role == "assistant" and text:
            latest_assistant = text
    if not seen_event:
        return None
    if session_id and not seen_matching_session:
        session_ids = {
            _string(json.loads(line).get("sessionId"))
            for line in lines
            if line.strip() and line.lstrip().startswith("{")
        }
        if any(value for value in session_ids):
            return None
    return {
        "cwd": cwd,
        "name": _preview(first_user),
        "latest_assistant_message": _preview(latest_assistant),
        "created_at_ms": first_timestamp_ms,
        "updated_at_ms": latest_timestamp_ms,
    }


_HARNESS_MARKUP_TAGS = (
    "system-reminder",
    "local-command-caveat",
    "command-name",
    "command-message",
    "command-args",
    "command-contents",
    "local-command-stdout",
)
_HARNESS_MARKUP_SPAN_RE = re.compile(
    r"<(" + "|".join(_HARNESS_MARKUP_TAGS) + r")>.*?</\1>",
    re.DOTALL,
)
_HARNESS_MARKUP_TAG_RE = re.compile(r"</?(" + "|".join(_HARNESS_MARKUP_TAGS) + r")>")


def _meaningful_user_text(text: str) -> str | None:
    """Return user-authored text with harness-injected markup removed."""
    cleaned = _HARNESS_MARKUP_SPAN_RE.sub(" ", text)
    cleaned = _HARNESS_MARKUP_TAG_RE.sub(" ", cleaned)
    cleaned = cleaned.strip()
    return cleaned or None


def _message_text(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts) if parts else None


def _timestamp_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def _mtime_ms(path: Path) -> int:
    return int(path.stat().st_mtime * 1000)


def _preview(text: str | None, limit: int = 180) -> str | None:
    if text is None:
        return None
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."


def _decode_project_key(value: str) -> str | None:
    if not value.startswith("-"):
        return None
    return "/" + value[1:].replace("-", "/")


def _iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return (
        datetime.fromtimestamp(value / 1000, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
