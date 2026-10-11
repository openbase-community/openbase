"""Resolve a session for ``threads send``, deliver the message, wait for the reply.

Delivery picks the path the backend offers: a Claude Code terminal session
gets the message through its inbox socket (``super_agents.claude_inbox``);
every other thread goes through the local Openbase server's thread API,
which steers a busy turn and starts a new turn on an idle thread.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

import click
from super_agents.claude_inbox import deliver_steer

from openbase_coder_cli.cli.local_server import local_server_request
from openbase_coder_cli.terminal_sessions import (
    ClaudeTerminal,
    TerminalSession,
    row_time,
    thread_is_busy,
    thread_name,
)

SEND_TIMEOUT_SECONDS = 60.0
WAIT_POLL_SECONDS = 2.0
# Sender label the receiving Claude Code session shows for the message.
INBOX_FROM_NAME = "openbase-coder"
# Server messages that mean the busy/idle guess raced a turn boundary.
_ACTIVE_TURN_MARKER = "already has an active turn"
_NO_ACTIVE_TURN_MARKER = "no active turn"


# --- resolution ---------------------------------------------------------------------


class Target:
    """What ``send`` resolved: a terminal session and/or a thread row."""

    def __init__(
        self,
        *,
        session: TerminalSession | None,
        row: dict[str, Any] | None,
        inbox: ClaudeTerminal | None,
    ) -> None:
        self.session = session
        self.row = row
        self.inbox = inbox

    @property
    def thread_id(self) -> str | None:
        if self.session and self.session.thread_id:
            return self.session.thread_id
        if self.row:
            return str(self.row.get("thread_id"))
        return None

    @property
    def name(self) -> str:
        if self.session:
            return self.session.name
        if self.row:
            return thread_name(self.row) or str(self.row.get("thread_id"))
        return "?"

    @property
    def backend(self) -> str:
        if self.session:
            return self.session.backend
        return str((self.row or {}).get("backend") or "")

    @property
    def busy(self) -> bool:
        if self.session and self.session.busy is not None:
            return self.session.busy
        return thread_is_busy(self.row or {})


def _matches(query: str, *candidates: str | None) -> tuple[bool, bool, bool]:
    """(exact, case-insensitive exact, substring) against the candidates."""
    folded = query.casefold()
    values = [value for value in candidates if value]
    return (
        any(value == query for value in values),
        any(value.casefold() == folded for value in values),
        any(folded in value.casefold() for value in values),
    )


def resolve_target(
    query: str,
    sessions: list[TerminalSession],
    threads: list[dict[str, Any]],
    terminals: list[ClaudeTerminal],
) -> Target:
    """Pick the session or thread ``query`` names.

    Terminal sessions win over other threads at every tier: exact id or name,
    then case-insensitive name, then a unique substring of the name. A Claude
    session id (or its first characters) also works.
    """
    inbox_by_session = {
        terminal.record.session_id: terminal for terminal in terminals if terminal.live
    }
    query = query.strip()
    if not query:
        raise click.UsageError("Give a session name or thread id.")

    def session_target(session: TerminalSession) -> Target:
        row = next(
            (
                item
                for item in threads
                if session.thread_id and item.get("thread_id") == session.thread_id
            ),
            None,
        )
        inbox = (
            inbox_by_session.get(session.backend_session_id)
            if session.backend_session_id
            else None
        )
        return Target(session=session, row=row, inbox=inbox)

    for tier in range(3):
        hits = [
            session
            for session in sessions
            if _matches(
                query,
                session.thread_id,
                session.backend_session_id,
                session.name,
            )[tier]
            or (
                tier == 0
                and session.backend_session_id is not None
                and len(query) >= 8
                and session.backend_session_id.startswith(query)
            )
        ]
        if len(hits) == 1:
            return session_target(hits[0])
        if len(hits) > 1:
            raise click.ClickException(_ambiguous(query, [hit.name for hit in hits]))
    for tier in range(3):
        hits = [
            row
            for row in threads
            if _matches(
                query,
                str(row.get("thread_id") or ""),
                str(row.get("backend_session_id") or ""),
                thread_name(row),
            )[tier]
        ]
        if len(hits) == 1:
            row = hits[0]
            inbox = inbox_by_session.get(str(row.get("backend_session_id") or ""))
            return Target(session=None, row=row, inbox=inbox)
        if len(hits) > 1:
            raise click.ClickException(
                _ambiguous(
                    query,
                    [thread_name(row) or str(row.get("thread_id")) for row in hits],
                )
            )
    raise click.ClickException(
        f"No session or thread matches {query!r}. `openbase-coder threads list` "
        "shows the terminal sessions on this computer; `--all` shows every thread."
    )


def _ambiguous(query: str, names: list[str]) -> str:
    shown = ", ".join(sorted(set(names))[:8])
    return f"{query!r} matches more than one session ({shown}); use the thread id."


# --- delivery ---------------------------------------------------------------------------


def _thread_api(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    response = local_server_request(
        method,
        path,
        ok_statuses=(400, 404, 409),
        timeout=SEND_TIMEOUT_SECONDS,
        **kwargs,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": response.text.strip() or f"HTTP {response.status_code}"}
    return response.status_code, payload if isinstance(payload, dict) else {}


def _server_error(payload: dict[str, Any]) -> str:
    return str(payload.get("error") or payload.get("detail") or "The request failed.")


def send_via_server(thread_id: str, text: str, *, busy: bool) -> dict[str, Any]:
    """Steer the active turn when busy, otherwise start a new turn.

    The busy guess can race a turn boundary; the server's refusal says which
    way it went, and the other path is tried once.
    """
    base = f"/api/threads/{quote(thread_id, safe='')}/turns/"
    order = ["steer", "start"] if busy else ["start", "steer"]
    last_error = "The request failed."
    for attempt, action in enumerate(order):
        path = base + "steer/" if action == "steer" else base
        status, payload = _thread_api("POST", path, json={"prompt": text})
        if status < 400:
            return {"delivery": action, **payload}
        last_error = _server_error(payload)
        lowered = last_error.lower()
        retry = (action == "start" and _ACTIVE_TURN_MARKER in lowered) or (
            action == "steer" and _NO_ACTIVE_TURN_MARKER in lowered
        )
        if attempt == 0 and retry:
            continue
        break
    raise click.ClickException(last_error)


def send_via_inbox(terminal: ClaudeTerminal, text: str) -> dict[str, Any]:
    result = asyncio.run(
        deliver_steer(
            terminal.record,
            text,
            target_session_id=terminal.record.session_id,
            from_name=INBOX_FROM_NAME,
        )
    )
    if not result.written and not result.may_have_been_written:
        reasons = {
            "socket_unreachable": "its inbox socket no longer accepts connections (the session has probably ended)",
            "rejected_by_peer": "the session rejected the message (stale inbox record; start a new session with `openbase-coder claude`)",
            "write_failed": "the message could not be written to its inbox socket",
            "empty_text": "the message is empty",
        }
        raise click.ClickException(
            "Could not deliver to the Claude Code session: "
            + reasons.get(result.reason or "", result.reason or "unknown error")
        )
    return {"delivery": "inbox", **result.to_json()}


# --- waiting -------------------------------------------------------------------------------


def completed_reply(detail: dict[str, Any], *, since: float) -> dict[str, Any] | None:
    """The newest turn finished after ``since``, once the thread is idle.

    Keyed on completion rather than start: a steered turn began before the
    message was sent, and its end is still the reply being waited for.
    """
    if detail.get("current_turn"):
        return None
    history = detail.get("turn_history")
    if not isinstance(history, list):
        return None
    finished = [
        turn
        for turn in history
        if isinstance(turn, dict) and (row_time(turn, "completed_at") or 0) >= since
    ]
    if not finished:
        return None
    return max(finished, key=lambda turn: row_time(turn, "completed_at") or 0)


def wait_for_reply(
    thread_id: str,
    *,
    since: float,
    timeout: float,
    poll: float | None = None,
) -> dict[str, Any]:
    interval = WAIT_POLL_SECONDS if poll is None else poll
    deadline = time.monotonic() + timeout
    path = f"/api/threads/{quote(thread_id, safe='')}/"
    while True:
        status, detail = _thread_api("GET", path)
        if status >= 400:
            raise click.ClickException(_server_error(detail))
        reply = completed_reply(detail, since=since)
        if reply is not None:
            return reply
        if time.monotonic() >= deadline:
            raise click.ClickException(
                f"No reply within {int(timeout)}s; the turn may still be running "
                f"(`openbase-coder threads list`)."
            )
        time.sleep(interval)
