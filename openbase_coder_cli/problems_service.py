"""Django-free helpers for the diagnostics "problems" datastore.

``openbase-coder report issue`` captures the most recent user interaction of a
thread — the latest user message plus the surrounding turn context and thread
metadata — and records it here so problematic interactions can be reviewed
later. Each problem is written as one JSON file under
:data:`openbase_coder_cli.paths.PROBLEMS_DIR`; the directory listing is the
source of truth, so there is no separate index to keep in sync.

The capture path talks to the running Codex app server for the real message
history, and degrades to the Super Agents state store's cached prompt preview
when the app server is unavailable, so recording a problem never hard-fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from super_agents.app_server_client import DEFAULT_STATE_FILE, CodexAppServerClient
from super_agents.session_history import read_session_messages
from super_agents.state import SessionRecord, read_state_file_locked

from openbase_coder_cli.paths import PROBLEMS_DIR

SUPER_AGENTS_STATE_FILE_ENV = "SUPER_AGENTS_STATE_FILE"

# Storage schema version, bumped when the on-disk record shape changes.
PROBLEM_SCHEMA_VERSION = 1

# The captured user message is stored in full up to this many characters; the
# surrounding interaction tail keeps the last N messages, each clipped, so a
# runaway thread cannot write an unbounded record.
PROBLEM_USER_MESSAGE_MAX_CHARS = 100_000
PROBLEM_INTERACTION_MAX_MESSAGES = 60
PROBLEM_INTERACTION_MESSAGE_MAX_CHARS = 20_000

# Filenames are timestamp-first so a directory listing sorts chronologically.
PROBLEM_ID_RE = re.compile(r"^prob-\d{8}T\d{6}Z-[0-9a-f]{8}$")

# Errors the app-server read can legitimately raise when a thread is idle,
# not yet materialized, or the app server is not running. Any of these falls
# back to the state store's cached preview rather than failing the capture.
_APP_SERVER_READ_ERRORS = (RuntimeError, ValueError, OSError, ConnectionError)


@dataclass(frozen=True)
class ThreadMetadata:
    """Metadata about the thread a problem was captured from."""

    thread_id: str
    label: str | None = None
    agent_name: str | None = None
    cwd: str | None = None
    updated_at: str | None = None
    last_event_at: str | None = None
    last_turn_id: str | None = None


@dataclass(frozen=True)
class CapturedMessage:
    """The most recent user message captured for a problem."""

    text: str
    source: str
    truncated: bool = False


@dataclass(frozen=True)
class ProblemRecord:
    """One recorded problematic interaction."""

    id: str
    created_at: str
    thread: ThreadMetadata
    user_message: CapturedMessage
    schema_version: int = PROBLEM_SCHEMA_VERSION
    note: str | None = None
    reported_by: str | None = None
    interaction: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        # Drop empty optional fields so records stay readable.
        if not payload.get("note"):
            payload.pop("note", None)
        if not payload.get("reported_by"):
            payload.pop("reported_by", None)
        if not payload.get("interaction"):
            payload.pop("interaction", None)
        return payload


def _problems_dir() -> Path:
    return PROBLEMS_DIR


def _super_agents_state_path() -> Path:
    configured = os.environ.get(SUPER_AGENTS_STATE_FILE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_STATE_FILE


def _session_activity(session: SessionRecord) -> str:
    """Sort key for "most recently active" — newest activity string wins.

    ISO-8601 timestamps sort correctly lexicographically, so comparing the
    strings directly avoids parsing every candidate.
    """
    return max(
        session.last_event_at or "",
        session.last_finished_at or "",
        session.last_started_at or "",
        session.updated_at or "",
    )


def resolve_target_session(
    thread_id: str | None = None,
    *,
    state_path: Path | None = None,
) -> tuple[str, SessionRecord | None]:
    """Resolve which thread ``report issue`` should capture.

    With an explicit ``thread_id`` the matching session record is returned when
    present (a record is not required — the app server may still know the
    thread). Otherwise the most recently active session in the Super Agents
    state store is selected, which is the interaction currently in flight on
    this machine.
    """
    state = read_state_file_locked(state_path or _super_agents_state_path())
    if thread_id:
        return thread_id, state.sessions.get(thread_id)
    if not state.sessions:
        raise LookupError(
            "No thread activity was found to report. Start or resume a thread "
            "first, or pass --thread-id explicitly."
        )
    session = max(state.sessions.values(), key=_session_activity)
    return session.thread_id, session


def _thread_metadata(thread_id: str, session: SessionRecord | None) -> ThreadMetadata:
    if session is None:
        return ThreadMetadata(thread_id=thread_id)
    return ThreadMetadata(
        thread_id=thread_id,
        label=session.label,
        agent_name=session.agent_name,
        cwd=session.cwd,
        updated_at=session.updated_at,
        last_event_at=session.last_event_at,
        last_turn_id=session.last_turn_id,
    )


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _latest_user_text(messages: list[dict[str, str]]) -> str | None:
    for message in reversed(messages):
        if message.get("role") == "user":
            text = (message.get("text") or "").strip()
            if text:
                return text
    return None


def _interaction_tail(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """The last user message and everything after it, bounded and clipped.

    This is the "problematic interaction" a reviewer wants: the prompt that
    triggered the turn plus the agent/tool activity it produced.
    """
    start = 0
    for index in range(len(messages) - 1, -1, -1):
        if (
            messages[index].get("role") == "user"
            and (messages[index].get("text") or "").strip()
        ):
            start = index
            break
    tail = messages[start:][:PROBLEM_INTERACTION_MAX_MESSAGES]
    bounded: list[dict[str, str]] = []
    for message in tail:
        text, _ = _clip(
            message.get("text") or "", PROBLEM_INTERACTION_MESSAGE_MAX_CHARS
        )
        bounded.append({"role": str(message.get("role") or "unknown"), "text": text})
    return bounded


def _preview_user_text(session: SessionRecord | None) -> str | None:
    """Fallback user message from the state store's cached turn preview."""
    if session is None or not session.turns:
        return None
    for turn in reversed(list(session.turns.values())):
        preview = (turn.prompt_preview or "").strip()
        if preview:
            return preview
    return None


ClientFactory = Callable[[], CodexAppServerClient]


async def _read_messages_via_app_server(
    thread_id: str,
    client_factory: ClientFactory,
) -> list[dict[str, str]]:
    client = client_factory()
    try:
        await client.ensure_connected()
        return await read_session_messages(client, thread_id)
    finally:
        await client.close()


async def acapture_problem(
    *,
    thread_id: str | None = None,
    note: str | None = None,
    reported_by: str | None = None,
    state_path: Path | None = None,
    client_factory: ClientFactory | None = None,
    now: datetime | None = None,
    messages_reader: Callable[[str], Awaitable[list[dict[str, str]]]] | None = None,
) -> ProblemRecord:
    """Capture the most recent user interaction of a thread into a record.

    The record is returned but not written; call :func:`write_problem_record`
    to persist it. ``client_factory``/``messages_reader``/``now`` are injectable
    for testing.
    """
    resolved_id, session = resolve_target_session(thread_id, state_path=state_path)
    metadata = _thread_metadata(resolved_id, session)

    if messages_reader is None:
        factory = client_factory or CodexAppServerClient

        async def _default_reader(tid: str) -> list[dict[str, str]]:
            return await _read_messages_via_app_server(tid, factory)

        reader = _default_reader
    else:
        reader = messages_reader

    messages: list[dict[str, str]] = []
    try:
        messages = await reader(resolved_id)
    except _APP_SERVER_READ_ERRORS:
        messages = []

    interaction: list[dict[str, str]] = []
    user_text = _latest_user_text(messages)
    if user_text is not None:
        source = "app_server"
        interaction = _interaction_tail(messages)
    else:
        # App server had nothing usable — fall back to the cached preview.
        user_text = _preview_user_text(session)
        source = "state_preview"

    if user_text is None:
        raise LookupError(
            f"No user message could be found for thread {resolved_id}. The "
            "thread may not have received a prompt yet."
        )

    clipped_text, truncated = _clip(user_text, PROBLEM_USER_MESSAGE_MAX_CHARS)
    moment = now or datetime.now(UTC)
    return ProblemRecord(
        id=_new_problem_id(moment),
        created_at=moment.isoformat(),
        thread=metadata,
        user_message=CapturedMessage(
            text=clipped_text, source=source, truncated=truncated
        ),
        note=(note.strip() or None) if note else None,
        reported_by=(reported_by.strip() or None) if reported_by else None,
        interaction=interaction,
    )


def capture_problem(
    *,
    thread_id: str | None = None,
    note: str | None = None,
    reported_by: str | None = None,
    state_path: Path | None = None,
    client_factory: ClientFactory | None = None,
) -> ProblemRecord:
    """Synchronous wrapper around :func:`acapture_problem`."""
    return asyncio.run(
        acapture_problem(
            thread_id=thread_id,
            note=note,
            reported_by=reported_by,
            state_path=state_path,
            client_factory=client_factory,
        )
    )


def _new_problem_id(moment: datetime) -> str:
    stamp = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"prob-{stamp}-{uuid.uuid4().hex[:8]}"


def write_problem_record(record: ProblemRecord) -> Path:
    """Persist a problem record as one JSON file and return its path."""
    directory = _problems_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record.id}.json"
    payload = json.dumps(record.to_json(), indent=2, ensure_ascii=False)
    # Atomic write so a concurrent listing never reads a half-written file.
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def list_problem_records() -> list[dict[str, Any]]:
    """Return recorded problems, newest first."""
    directory = _problems_dir()
    if not directory.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in directory.glob("prob-*.json"):
        record = _load_record(path)
        if record is not None:
            records.append(record)
    records.sort(key=lambda item: str(item.get("id") or ""), reverse=True)
    return records


def resolve_problem_record(identifier: str) -> dict[str, Any]:
    """Load one problem record by id or file path."""
    directory = _problems_dir()
    candidate = directory / f"{identifier}.json"
    if not candidate.exists():
        as_path = Path(identifier).expanduser()
        if as_path.exists():
            candidate = as_path
    record = _load_record(candidate)
    if record is None:
        raise FileNotFoundError(f"No problem record found for {identifier!r}.")
    return record


def _load_record(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
