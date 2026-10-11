from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from openbase_coder_cli.cli.utils import get_data_dir
from openbase_coder_cli.json_snapshot import JsonFileSnapshot

VOICE_HISTORY_FILE = "openbase-voice-assignments.json"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceHistoryEntry:
    thread_id: str
    agent_name: str | None
    cwd: str | None
    voice_id: str | None
    voice_name: str | None
    kind: str
    source: str
    first_seen_at: float
    last_seen_at: float


class AgentVoiceLookupError(ValueError):
    """Base error for resolving an agent name to a LiveKit voice."""


class UnknownAgentVoiceError(AgentVoiceLookupError):
    """No active voice assignment exists for the requested agent name."""


class AmbiguousAgentVoiceError(AgentVoiceLookupError):
    """More than one active voice assignment exists for an agent name."""

    def __init__(
        self, agent_name: str, candidates: tuple[VoiceHistoryEntry, ...]
    ) -> None:
        self.agent_name = agent_name
        self.candidates = candidates
        candidate_ids = ", ".join(candidate.thread_id for candidate in candidates)
        super().__init__(
            f"Agent name {agent_name!r} is ambiguous. Candidate thread IDs: {candidate_ids}."
        )


def get_voice_history_entry(thread_id: str | None) -> VoiceHistoryEntry | None:
    if not thread_id:
        return None
    payload = _read_history()
    entry = payload.get("threads", {}).get(thread_id)
    return _entry_from_payload(entry) if isinstance(entry, dict) else None


def get_voice_history_entry_for_agent_name(agent_name: str | None) -> VoiceHistoryEntry:
    normalized = _normalize_agent_name(agent_name)
    if not normalized:
        logger.warning(
            "livekit_voice_lookup_missing_agent_name history_path=%s requested_agent_name=%r",
            _history_path(),
            agent_name,
        )
        raise UnknownAgentVoiceError("Agent name is required.")

    payload = _read_history()
    candidates = _thread_agent_entries(payload, normalized)
    lookup_source = "voice_assignments" if candidates else "none"
    if not candidates:
        backfilled = _backfill_agent_entries_from_super_agents_state(normalized)
        if backfilled:
            payload = _read_history()
            candidates = _thread_agent_entries(payload, normalized)
            lookup_source = "super_agents_state" if candidates else "none"
    if not candidates:
        logger.warning(
            "livekit_voice_lookup_miss requested_agent_name=%r normalized_agent_name=%s "
            "history_path=%s thread_agents=%s super_agents_state_matches=%s",
            agent_name,
            normalized,
            _history_path(),
            _thread_agent_name_summary(payload),
            _super_agents_state_agent_matches(normalized),
        )
        raise UnknownAgentVoiceError(
            f"Agent {agent_name!r} does not have an active voice assignment."
        )
    if len(candidates) > 1:
        selected = candidates[0]
        logger.info(
            "livekit_voice_lookup_multiple requested_agent_name=%r normalized_agent_name=%s "
            "history_path=%s selected=%s candidates=%s",
            agent_name,
            normalized,
            _history_path(),
            _entry_log_payload(selected),
            [_entry_log_payload(candidate) for candidate in candidates],
        )
        return selected
    logger.info(
        "livekit_voice_lookup_hit requested_agent_name=%r normalized_agent_name=%s "
        "source=%s history_path=%s entry=%s",
        agent_name,
        normalized,
        lookup_source,
        _history_path(),
        _entry_log_payload(candidates[0]),
    )
    return candidates[0]


def record_voice_assignment(
    *,
    thread_id: str | None,
    agent_name: str | None,
    cwd: str | None,
    voice_id: str | None,
    voice_name: str | None,
    kind: str,
    source: str,
    seen_at: float | None = None,
) -> VoiceHistoryEntry | None:
    if not thread_id:
        logger.info(
            "livekit_voice_assignment_skipped reason=missing_thread_id source=%s "
            "agent_name=%r voice_id=%r voice_name=%r",
            source,
            agent_name,
            voice_id,
            voice_name,
        )
        return None

    timestamp = seen_at or time.time()
    payload = _read_history_fresh()
    threads = payload.setdefault("threads", {})
    previous = _entry_from_payload(threads.get(thread_id))
    entry = VoiceHistoryEntry(
        thread_id=thread_id,
        agent_name=_optional_str(agent_name)
        or (previous.agent_name if previous else None),
        cwd=_optional_str(cwd) or (previous.cwd if previous else None),
        voice_id=_optional_str(voice_id) or (previous.voice_id if previous else None),
        voice_name=_optional_str(voice_name)
        or (previous.voice_name if previous else None),
        kind=_optional_str(kind) or (previous.kind if previous else "codex_thread"),
        source=_optional_str(source) or (previous.source if previous else "unknown"),
        first_seen_at=previous.first_seen_at if previous else timestamp,
        last_seen_at=timestamp,
    )
    threads[thread_id] = asdict(entry)
    _write_history(payload)
    logger.info(
        "livekit_voice_assignment_recorded thread_id=%s source=%s kind=%s "
        "agent_name=%r normalized_agent_name=%s voice_id=%r voice_name=%r "
        "history_path=%s",
        thread_id,
        entry.source,
        entry.kind,
        entry.agent_name,
        _normalize_agent_name(entry.agent_name),
        entry.voice_id,
        entry.voice_name,
        _history_path(),
    )
    return entry


def voice_history_debug_snapshot(agent_name: str | None = None) -> dict[str, Any]:
    payload = _read_history()
    normalized = _normalize_agent_name(agent_name)
    return {
        "history_path": str(_history_path()),
        "requested_agent_name": agent_name,
        "normalized_agent_name": normalized,
        "thread_matches": [
            _entry_log_payload(entry)
            for entry in _thread_agent_entries(payload, normalized)
        ]
        if normalized
        else [],
        "thread_agents": _thread_agent_name_summary(payload),
        "super_agents_state_matches": _super_agents_state_agent_matches(normalized)
        if normalized
        else [],
    }


def _backfill_agent_entries_from_super_agents_state(
    normalized_agent_name: str,
) -> tuple[VoiceHistoryEntry, ...]:
    matches = _super_agents_state_agent_matches(normalized_agent_name)
    if not matches:
        logger.info(
            "livekit_voice_assignment_backfill_miss normalized_agent_name=%s "
            "super_agents_state_path=%s",
            normalized_agent_name,
            _super_agents_state_path(),
        )
        return ()

    from openbase_coder_cli.livekit_voice_route import super_agent_voice_for_context

    entries: list[VoiceHistoryEntry] = []
    for match in matches:
        try:
            voice = super_agent_voice_for_context(
                match.get("thread_id"),
                match.get("label"),
                match.get("agent_name"),
            )
        except Exception:
            logger.debug(
                "Unable to resolve Super Agents state voice for match=%s",
                match,
                exc_info=True,
            )
            voice = None

        entry = record_voice_assignment(
            thread_id=_optional_str(match.get("thread_id")),
            agent_name=_optional_str(match.get("agent_name")),
            cwd=_optional_str(match.get("cwd")),
            voice_id=voice.voice_id if voice else _optional_str(match.get("voice_id")),
            voice_name=voice.name if voice else _optional_str(match.get("voice_name")),
            kind=_optional_str(match.get("kind")) or "codex_thread",
            source=_optional_str(match.get("source")) or "super_agents_state",
        )
        if entry is not None:
            entries.append(entry)

    logger.info(
        "livekit_voice_assignment_backfilled normalized_agent_name=%s "
        "super_agents_state_path=%s entries=%s",
        normalized_agent_name,
        _super_agents_state_path(),
        [_entry_log_payload(entry) for entry in entries],
    )
    return tuple(entries)


def _history_path() -> Path:
    return get_data_dir() / VOICE_HISTORY_FILE


def _read_history() -> dict[str, Any]:
    """Read-only snapshot shared across callers; never mutate the result."""
    return _history_snapshot.get(_history_path())


def _read_history_fresh() -> dict[str, Any]:
    """Private, freshly parsed copy for read-modify-write paths."""
    return _history_snapshot.read_fresh(_history_path())


def _parse_history_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"threads": {}}
    if not isinstance(payload.get("threads"), dict):
        payload["threads"] = {}
    return payload


_history_snapshot: JsonFileSnapshot[dict[str, Any]] = JsonFileSnapshot(
    _parse_history_payload
)


def _thread_agent_entries(
    payload: dict[str, Any],
    normalized_agent_name: str,
) -> tuple[VoiceHistoryEntry, ...]:
    threads = payload.get("threads")
    if not isinstance(threads, dict):
        return ()
    return _dedupe_entries(
        entry
        for entry in (_entry_from_payload(value) for value in threads.values())
        if entry is not None
        and entry.voice_id
        and _normalize_agent_name(entry.agent_name) == normalized_agent_name
    )


def _dedupe_entries(entries) -> tuple[VoiceHistoryEntry, ...]:
    by_thread_id: dict[str, VoiceHistoryEntry] = {}
    for entry in entries:
        by_thread_id[entry.thread_id] = entry
    return tuple(
        sorted(
            by_thread_id.values(),
            key=lambda entry: (entry.last_seen_at, entry.thread_id),
            reverse=True,
        )
    )


def _write_history(payload: dict[str, Any]) -> None:
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    threads = payload.get("threads", {})
    payload = {"threads": threads if isinstance(threads, dict) else {}}
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _history_snapshot.invalidate(path)


def _thread_agent_name_summary(payload: dict[str, Any]) -> list[dict[str, Any]]:
    threads = payload.get("threads")
    if not isinstance(threads, dict):
        return []
    entries = [
        entry
        for entry in (_entry_from_payload(value) for value in threads.values())
        if entry is not None and entry.agent_name
    ]
    return [
        {
            "thread_id": entry.thread_id,
            "agent_name": entry.agent_name,
            "normalized_agent_name": _normalize_agent_name(entry.agent_name),
            "voice_id": entry.voice_id,
            "voice_name": entry.voice_name,
            "source": entry.source,
            "kind": entry.kind,
        }
        for entry in sorted(
            entries, key=lambda candidate: candidate.last_seen_at, reverse=True
        )[:20]
    ]


def _super_agents_state_path() -> Path:
    return Path(
        os.environ.get("SUPER_AGENTS_STATE_FILE")
        or Path.home() / ".super-agents" / "state.json"
    )


def _super_agents_state_agent_matches(
    normalized_agent_name: str,
) -> list[dict[str, Any]]:
    return sorted(
        [
            *_json_super_agents_state_agent_matches(normalized_agent_name),
            *_claude_code_state_agent_matches(normalized_agent_name),
        ],
        key=lambda match: (
            str(match.get("last_started_at") or ""),
            str(match.get("updated_at") or ""),
        ),
        reverse=True,
    )


def _json_super_agents_state_agent_matches(
    normalized_agent_name: str,
) -> list[dict[str, Any]]:
    path = _super_agents_state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, dict):
        return []

    matches: list[dict[str, Any]] = []
    for thread_id, value in sessions.items():
        if not isinstance(value, dict):
            continue
        agent_name = _optional_str(value.get("agentName"))
        if _normalize_agent_name(agent_name) != normalized_agent_name:
            continue
        matches.append(
            {
                "thread_id": _optional_str(value.get("threadId"))
                or _optional_str(thread_id),
                "agent_name": agent_name,
                "normalized_agent_name": normalized_agent_name,
                "label": _optional_str(value.get("label")),
                "cwd": _optional_str(value.get("cwd")),
                "voice_id": _optional_str(value.get("voiceId")),
                "voice_name": _optional_str(value.get("voiceName")),
                "last_status": _optional_str(value.get("lastStatus")),
                "updated_at": _optional_str(value.get("updatedAt")),
                "last_started_at": _optional_str(value.get("lastStartedAt")),
                "kind": "codex_thread",
                "source": "super_agents_state",
            }
        )
    return matches


def _claude_code_state_path() -> Path:
    configured_home = os.environ.get("SUPER_AGENTS_CLAUDE_CODE_HOME")
    if configured_home:
        return Path(configured_home).expanduser() / "state.sqlite3"
    return Path.home() / ".local" / "share" / "super-agents-claude-code" / "state.sqlite3"


def _claude_code_state_agent_matches(
    normalized_agent_name: str,
) -> list[dict[str, Any]]:
    path = _claude_code_state_path()
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select id, name, agent_name, cwd, status, updated_at, created_at
                from sessions
                where lower(trim(agent_name)) = ?
                order by updated_at desc
                limit 20
                """,
                (normalized_agent_name,),
            ).fetchall()
    except sqlite3.Error:
        logger.debug(
            "Unable to read Claude Code Super Agents state path=%s",
            path,
            exc_info=True,
        )
        return []

    return [
        {
            "thread_id": _optional_str(row["id"]),
            "agent_name": _optional_str(row["agent_name"]),
            "normalized_agent_name": normalized_agent_name,
            "label": _optional_str(row["name"]),
            "cwd": _optional_str(row["cwd"]),
            "voice_id": None,
            "voice_name": None,
            "last_status": _optional_str(row["status"]),
            "updated_at": _optional_str(row["updated_at"]),
            "last_started_at": _optional_str(row["created_at"]),
            "kind": "codex_thread",
            "source": "claude_code_state",
        }
        for row in rows
    ]


def _entry_log_payload(entry: VoiceHistoryEntry) -> dict[str, Any]:
    return {
        "thread_id": entry.thread_id,
        "agent_name": entry.agent_name,
        "normalized_agent_name": _normalize_agent_name(entry.agent_name),
        "voice_id": entry.voice_id,
        "voice_name": entry.voice_name,
        "kind": entry.kind,
        "source": entry.source,
        "last_seen_at": entry.last_seen_at,
    }


def _entry_from_payload(value: Any) -> VoiceHistoryEntry | None:
    if not isinstance(value, dict):
        return None
    thread_id = _optional_str(value.get("thread_id"))
    if not thread_id:
        return None
    first_seen_at = value.get("first_seen_at")
    last_seen_at = value.get("last_seen_at")
    now = time.time()
    return VoiceHistoryEntry(
        thread_id=thread_id,
        agent_name=_optional_str(value.get("agent_name")),
        cwd=_optional_str(value.get("cwd")),
        voice_id=_optional_str(value.get("voice_id")),
        voice_name=_optional_str(value.get("voice_name")),
        kind=_optional_str(value.get("kind")) or "codex_thread",
        source=_optional_str(value.get("source")) or "unknown",
        first_seen_at=first_seen_at if isinstance(first_seen_at, (int, float)) else now,
        last_seen_at=last_seen_at if isinstance(last_seen_at, (int, float)) else now,
    )


def _optional_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalize_agent_name(value: Any) -> str:
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""
