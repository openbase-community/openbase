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

from openbase_coder_cli.backend_auth import backend_auth_failure
from openbase_coder_cli.cloud_model_errors import model_proxy_denial_spoken_message
from openbase_coder_cli.livekit_agent.codex_turns import (
    _speech_excerpt,
)
from openbase_coder_cli.livekit_agent.super_agents_client_common import (
    DISPATCH_TIMING_LOG,
    logger,
)


def _turn_failed(progress: dict[str, Any]) -> bool:
    summary = progress.get("summary")
    status = progress.get("status") or (
        summary.get("status") if isinstance(summary, dict) else None
    )
    return isinstance(status, str) and status.lower() == "failed"


def _without_cached_messages(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_cached_messages(item)
            for key, item in value.items()
            if key != "lastUsefulMessage"
        }
    if isinstance(value, list):
        return [_without_cached_messages(item) for item in value]
    return value


def _speech_text_from_progress(
    progress: dict[str, Any],
    *,
    turn_scoped: bool = False,
    turn_id: str | None = None,
) -> str:
    """Pick the speakable assistant text out of a progress snapshot.

    ``turn_scoped`` restricts the search to text the streamed turn produced
    itself, for MID-TURN commentary. While a new turn runs, the session-level
    ``lastUsefulMessage`` and the ``turns``/``recentTurns`` lists still carry
    EARLIER turns' answers and the running turn has none of its own yet; the
    unscoped fallbacks picked those up and the live voice bridge relayed the
    previous answer as commentary for a new question (2026-10-08: "what files
    are on my desktop" was answered with "two plus two is four" first). When
    ``turn_id`` is given, a turn or summary that names a different turn is
    ignored as well.
    """
    # Claude stores the failed turn's own proxy error in lastUsefulMessage.
    # Recover only a recognized denial positively tied to the requested turn;
    # session caches and earlier turns must never become the current answer.
    current_turn_id = turn_id or progress.get("turnId")
    if current_turn_id:
        for key in ("turn", "summary"):
            turn = progress.get(key)
            if isinstance(turn, dict) and _turn_identifier(turn) == current_turn_id:
                if denial := model_proxy_denial_spoken_message(
                    _own_last_useful_message(turn)
                ):
                    return denial
                # A dead Claude Code / Codex login is the turn's own answer
                # (Claude) or error (Codex); keep it so the caller can
                # classify it and say how to sign back in.
                for own_text in (
                    _own_last_useful_message(turn),
                    _own_string(turn, "lastError"),
                ):
                    if backend_auth_failure(own_text):
                        return own_text

    # Other lastUsefulMessage values on failed turns may be stale session
    # answers. Strip them recursively; fresh item-derived text remains usable.
    if _turn_failed(progress):
        progress = _without_cached_messages(progress)

    if turn_scoped:
        candidates = _turn_scoped_speech_candidates(progress, turn_id)
    else:
        candidates = _speech_candidates(progress)
    return _select_speech_candidate(candidates, progress)


def _speech_candidates(progress: dict[str, Any]) -> list[tuple[str, Any, bool]]:
    from super_agents.app_formatting import find_turn_useful_text

    summary = progress.get("summary")
    candidates: list[tuple[str, Any, bool]] = [
        (
            "summary.items.final_answers",
            _speech_text_from_turn_items(summary.get("items"))
            if isinstance(summary, dict)
            else None,
            True,
        ),
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
    return candidates


def _turn_scoped_speech_candidates(
    progress: dict[str, Any], turn_id: str | None
) -> list[tuple[str, Any, bool]]:
    """Only sources that belong to the streamed turn itself.

    Never the session-level ``lastUsefulMessage`` nor the ``turns`` /
    ``recentTurns`` lists (they hold earlier turns' answers), and only the
    turn's OWN ``lastUsefulMessage`` key (``_last_useful_message`` would
    recurse into nested lists). A ``summary`` is the streamed turn's (Codex
    builds it from the requested turn and stamps its id), so its own items and
    preview count unless it names another turn.
    """
    from super_agents.app_formatting import find_turn_useful_text

    candidates: list[tuple[str, Any, bool]] = []
    summary = progress.get("summary")
    if isinstance(summary, dict) and _belongs_to_turn(summary, turn_id):
        candidates.extend(
            [
                (
                    "summary.items.final_answers",
                    _speech_text_from_turn_items(summary.get("items")),
                    True,
                ),
                ("summary.items", find_turn_useful_text(summary.get("items")), True),
            ]
        )
    turn = progress.get("turn")
    if isinstance(turn, dict) and _belongs_to_turn(turn, turn_id):
        candidates.extend(
            [
                (
                    "progress.turn.lastUsefulMessage",
                    _own_last_useful_message(turn),
                    True,
                ),
                ("progress.turn", find_turn_useful_text(turn), True),
            ]
        )
    if isinstance(summary, dict) and turn_id and _turn_identifier(summary) == turn_id:
        # Codex's summary preview is the requested turn's own text; only
        # trust it when the summary positively names the streamed turn.
        candidates.append(
            ("summary.lastUsefulMessage", summary.get("lastUsefulMessage"), True)
        )
    return candidates


def _turn_identifier(value: dict[str, Any]) -> str | None:
    for key in ("turnId", "id"):
        identifier = value.get(key)
        if isinstance(identifier, str) and identifier:
            return identifier
    return None


def _belongs_to_turn(value: dict[str, Any], turn_id: str | None) -> bool:
    identifier = _turn_identifier(value)
    return not turn_id or identifier is None or identifier == turn_id


def _own_last_useful_message(value: dict[str, Any]) -> str | None:
    return _own_string(value, "lastUsefulMessage")


def _own_string(value: dict[str, Any], key: str) -> str | None:
    text = value.get(key)
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


def _select_speech_candidate(
    candidates: list[tuple[str, Any, bool]], progress: dict[str, Any]
) -> str:
    from super_agents.app_formatting import find_useful_text

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
        return model_proxy_denial_spoken_message(text) or _speech_excerpt(text)
    return ""


def _speech_text_from_turn_items(value: Any) -> str | None:
    if not isinstance(value, list):
        return None

    final_texts: list[str] = []
    assistant_texts: list[str] = []
    for item in value:
        text = _assistant_message_text(item)
        if not text:
            continue
        phase = _message_phase(item)
        if phase == "finalanswer":
            final_texts.append(text)
        else:
            assistant_texts.append(text)

    if final_texts:
        return _join_distinct_speech_parts(final_texts)
    if assistant_texts:
        return assistant_texts[-1]
    return None


def _assistant_message_text(value: Any, depth: int = 0) -> str | None:
    if depth > 6 or value is None:
        return None
    if isinstance(value, list):
        for item in value:
            if text := _assistant_message_text(item, depth + 1):
                return text
        return None
    if not isinstance(value, dict):
        return None

    role = _message_role(value)
    if role in {"agent", "agentmessage", "assistant", "assistantmessage"}:
        return _text_content(value.get("text") or value.get("content"))
    if role in {"user", "usermessage"}:
        return None

    payload = value.get("payload")
    if isinstance(payload, dict):
        payload_type = _normalized_label(payload.get("type"))
        if payload_type in {"taskcomplete", "turncompleted"}:
            return _text_content(
                payload.get("last_agent_message")
                or payload.get("lastAgentMessage")
                or payload.get("message")
                or payload.get("text")
            )

    for key in ("payload", "item", "message", "turn", "items", "events", "messages"):
        if text := _assistant_message_text(value.get(key), depth + 1):
            return text
    return None


def _message_phase(value: Any, depth: int = 0) -> str | None:
    if depth > 6 or not isinstance(value, dict):
        return None
    phase = _normalized_label(value.get("phase"))
    if phase:
        return phase
    for key in ("payload", "item", "message"):
        if result := _message_phase(value.get(key), depth + 1):
            return result
    return None


def _message_role(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    role = _normalized_label(value.get("role"))
    item_type = _normalized_label(value.get("type"))
    if role:
        return role
    if item_type:
        return item_type
    payload = value.get("payload")
    return _message_role(payload) if isinstance(payload, dict) else None


def _normalized_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return "".join(char for char in value.lower() if char.isalnum()) or None


def _join_distinct_speech_parts(parts: list[str]) -> str:
    distinct: list[str] = []
    for part in parts:
        normalized = _normalize_speech_candidate(part)
        if not normalized:
            continue
        if distinct and normalized == _normalize_speech_candidate(distinct[-1]):
            continue
        distinct.append(part)
    return "\n\n".join(distinct) if distinct else ""


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
