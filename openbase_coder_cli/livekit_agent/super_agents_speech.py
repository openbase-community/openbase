"""Speech-text extraction helpers for the Super Agents LiveKit client.

These were already free functions at the bottom of `super_agents_client`; they
pull the useful assistant text out of a progress snapshot and filter out
non-speech noise (schema labels, identifiers, timestamps, echoes of the user's
own message). They are re-exported from `super_agents_client` for backwards
compatibility with tests and other importers.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from openbase_coder_cli.livekit_agent.codex_turns import (
    _speech_excerpt,
)
from openbase_coder_cli.livekit_agent.super_agents_client_common import (
    DISPATCH_TIMING_LOG,
    logger,
)


def _speech_text_from_progress(progress: dict[str, Any]) -> str:
    from super_agents.app_formatting import find_turn_useful_text, find_useful_text

    summary = progress.get("summary")
    candidates: list[tuple[str, Any, bool]] = [
        (
            "summary.items",
            find_turn_useful_text(summary.get("items"))
            if isinstance(summary, dict)
            else None,
            True,
        ),
        (
            "progress.turn.lastUsefulMessage",
            _last_useful_message(progress.get("turn")),
            True,
        ),
        (
            "progress.turns.lastUsefulMessage",
            _last_useful_message(progress.get("turns")),
            True,
        ),
        (
            "progress.recentTurns.lastUsefulMessage",
            _last_useful_message(progress.get("recentTurns")),
            True,
        ),
        ("progress.turn", find_turn_useful_text(progress.get("turn")), True),
        ("progress.turns", find_turn_useful_text(progress.get("turns")), True),
        (
            "progress.recentTurns",
            find_turn_useful_text(progress.get("recentTurns")),
            True,
        ),
    ]
    candidates.extend(
        [
            ("progress.lastUsefulMessage", progress.get("lastUsefulMessage"), True),
        ]
    )
    if isinstance(summary, dict):
        candidates.extend(
            [
                ("summary.lastUsefulMessage", summary.get("lastUsefulMessage"), True),
            ]
        )
    for source, candidate, role_selected in candidates:
        text = (
            str(candidate).strip()
            if role_selected and isinstance(candidate, str)
            else find_useful_text(candidate)
        )
        if not text:
            continue
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        if _should_ignore_speech_text(text, progress):
            logger.info(
                "%s stage=speech_candidate_rejected source=%s text_len=%d text_hash=%s",
                DISPATCH_TIMING_LOG,
                source,
                len(text),
                text_hash,
            )
            continue
        logger.info(
            "%s stage=speech_candidate_selected source=%s text_len=%d text_hash=%s",
            DISPATCH_TIMING_LOG,
            source,
            len(text),
            text_hash,
        )
        return _speech_excerpt(text)
    return ""


def _last_useful_message(value: Any, depth: int = 0) -> str | None:
    if value is None or depth > 6:
        return None
    if isinstance(value, list):
        for item in reversed(value):
            if result := _last_useful_message(item, depth + 1):
                return result
        return None
    if not isinstance(value, dict):
        return None
    text = value.get("lastUsefulMessage")
    if isinstance(text, str) and text.strip():
        return text.strip()
    for key in ("turn", "turns", "recentTurns", "summary"):
        if result := _last_useful_message(value.get(key), depth + 1):
            return result
    return None


def _should_ignore_speech_text(text: str, progress: dict[str, Any]) -> bool:
    normalized = _normalize_speech_candidate(text)
    if _looks_like_schema_label(normalized):
        return True
    if _looks_like_metadata_identifier(normalized):
        return True
    if _looks_like_timestamp(normalized):
        return True
    return normalized in _user_message_texts(progress)


def _user_message_texts(value: Any, depth: int = 0) -> set[str]:
    if value is None or depth > 8:
        return set()
    if isinstance(value, list):
        texts: set[str] = set()
        for item in value:
            texts.update(_user_message_texts(item, depth + 1))
        return texts
    if not isinstance(value, dict):
        return set()

    item_type = str(value.get("type") or value.get("role") or "").lower()
    if item_type in {"user", "usermessage"}:
        if text := _text_content(value.get("text") or value.get("content")):
            return {_normalize_speech_candidate(text)}

    texts: set[str] = set()
    for key, child in value.items():
        normalized_key = "".join(char for char in str(key).lower() if char.isalnum())
        if normalized_key in {"prompt", "promptpreview"}:
            if text := _text_content(child):
                texts.add(_normalize_speech_candidate(text))
            continue
        texts.update(_user_message_texts(child, depth + 1))
    return texts


def _text_content(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [
            item.get("text", "")
            for item in value
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return " ".join(part for part in parts if part).strip() or None
    return None


def _normalize_speech_candidate(text: str) -> str:
    return text.strip().rstrip(".!?").strip().lower()


def _looks_like_metadata_identifier(text: str) -> bool:
    compact = text.replace("-", "").replace(" ", "")
    if len(compact) >= 16 and re.fullmatch(r"[0-9a-f]+", compact):
        return True
    return bool(re.fullmatch(r"(?:[0-9a-f]{4,}[-\s]+){2,}[0-9a-f]{4,}", text))


def _looks_like_timestamp(text: str) -> bool:
    lowered = text.replace(" dot ", ".").replace(" z", "z")
    compact = re.sub(r"\s+", "", lowered)
    return bool(
        re.fullmatch(
            r"\d{4}[-/]?\d{2}[-/]?\d{2}t?\d{2}:?\d{2}(?::?\d{2})?(?:\.\d+)?z?",
            compact,
        )
    )


def _looks_like_schema_label(text: str) -> bool:
    compact = text.replace(" ", "").replace("_", "").replace("-", "")
    return compact in {
        "agentmessage",
        "assistantmessage",
        "usermessage",
        "toolcall",
        "functioncall",
        "completed",
        "running",
        "waiting",
        "queued",
    }


def _progress_has_pending_requests(progress: dict[str, Any]) -> bool:
    if progress.get("pendingRequests"):
        return True
    summary = progress.get("summary")
    if isinstance(summary, dict) and summary.get("pendingRequestCount"):
        return True
    tracked = progress.get("trackedTurn")
    if isinstance(tracked, dict):
        if tracked.get("pendingRequestCount"):
            return True
        pending = tracked.get("pendingRequests")
        if isinstance(pending, list) and pending:
            return True
    return False
