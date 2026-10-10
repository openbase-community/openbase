"""``openbase-coder threads list`` / ``threads send``: message a terminal session.

``list`` shows the Codex and Claude Code sessions open in terminals on this
computer (``openbase_coder_cli.terminal_sessions`` finds them). ``send``
delivers a message to one of them, or to any other thread Openbase knows
(``threads_delivery`` picks the path and waits for the reply).
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import click
from super_agents.claude_inbox import InboxRecord
from super_agents.claude_transcript import transcript_path

from openbase_coder_cli.cli.local_server import local_server_request
from openbase_coder_cli.cli.threads_delivery import (
    resolve_target,
    send_via_inbox,
    send_via_server,
    wait_for_reply,
)
from openbase_coder_cli.terminal_sessions import (
    CLAUDE_BACKEND,
    ClaudeTerminal,
    TerminalSession,
    claude_inbox_records,
    claude_terminals,
    iso_epoch,
    live_codex_tui_processes,
    row_time,
    terminal_sessions,
    thread_is_busy,
    thread_name,
)

THREAD_PAGE_SIZE = 100
# Threads are listed by recent activity; a terminal session idle for a while
# drifts down the list, so several pages may be needed to find it.
THREAD_PAGES = 5
DEFAULT_WAIT_SECONDS = 600.0


def _json_echo(value: Any) -> None:
    click.echo(json.dumps(value, indent=2, sort_keys=True))


def _home_relative(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


# --- gathering ---------------------------------------------------------------------


def fetch_threads(
    *,
    pages: int = THREAD_PAGES,
    enough: Callable[[list[dict[str, Any]]], bool] | None = None,
) -> list[dict[str, Any]]:
    """The newest thread rows the local Openbase server lists.

    Follows the server's ``next`` links (cursor pagination) for up to
    ``pages`` pages, stopping early once ``enough`` says the rows gathered so
    far cover what the caller is looking for.
    """
    rows: list[dict[str, Any]] = []
    path: str | None = f"/api/threads/?page_size={THREAD_PAGE_SIZE}"
    for _ in range(pages):
        if path is None:
            break
        payload = local_server_request("GET", path).json()
        threads = payload.get("threads") if isinstance(payload, dict) else None
        if not isinstance(threads, list):
            break
        rows.extend(row for row in threads if isinstance(row, dict))
        if enough is not None and enough(rows):
            break
        next_link = payload.get("next")
        path = _api_path(next_link) if isinstance(next_link, str) else None
    return rows


def _api_path(link: str) -> str | None:
    """The request path of a ``next`` link, whether relative or absolute."""
    parsed = urlsplit(link)
    if not parsed.path:
        return None
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def tracked_thread_starts() -> dict[str, float]:
    """``{thread_id: record created_at}`` for the threads Super Agents tracks.

    Super Agents records a thread when it starts one and also when it merely
    sends to one (``threads send`` included), so the record's age relative
    to the thread's own creation is what tells a dispatched thread from a
    terminal's (``terminal_sessions.started_by_super_agents``).
    """
    from super_agents.app_server_client import DEFAULT_STATE_FILE
    from super_agents.state import read_state_file

    configured = os.environ.get("SUPER_AGENTS_STATE_FILE", "").strip()
    path = Path(configured).expanduser() if configured else DEFAULT_STATE_FILE
    starts: dict[str, float] = {}
    for thread_id, record in read_state_file(path).sessions.items():
        created = iso_epoch(getattr(record, "created_at", None))
        if created is not None:
            starts[thread_id] = created
    return starts


def _transcript_for(record: InboxRecord) -> Path | None:
    return transcript_path(
        SimpleNamespace(backend_session_id=record.session_id, cwd=record.cwd)
    )


def gather(
    fetch: Callable[..., list[dict[str, Any]]] = fetch_threads,
) -> tuple[list[dict[str, Any]], list[TerminalSession], list[ClaudeTerminal]]:
    """Thread rows plus the terminal sessions joined onto them.

    Pages of threads are fetched until every live terminal has found its
    thread (or the page limit is reached); the join is then redone on the
    final rows.
    """
    now = time.time()
    processes = live_codex_tui_processes(now=now)
    terminals = claude_terminals(claude_inbox_records(), transcript_for=_transcript_for)
    tracked = tracked_thread_starts()

    def join(rows: list[dict[str, Any]]) -> list[TerminalSession]:
        return terminal_sessions(
            rows,
            codex_processes=processes,
            claude_terminals=terminals,
            tracked_starts=tracked,
        )

    # Rows come newest-activity first. Once a page's rows are all older than
    # the oldest live terminal, no later page can hold a terminal's thread:
    # a thread is touched after its terminal started.
    starts = [process.started_at for process in processes]
    starts += [
        terminal.record.recorded_at or now for terminal in terminals if terminal.live
    ]
    floor = min(starts, default=now) - 60.0

    def enough(rows: list[dict[str, Any]]) -> bool:
        if all(session.thread_id for session in join(rows)):
            return True
        page = rows[-THREAD_PAGE_SIZE:]
        return bool(page) and all(
            (row_time(row, "updated_at") or 0) < floor for row in page
        )

    try:
        threads = fetch(enough=enough)
    except click.ClickException as exc:
        # Terminal sessions are still discoverable from local facts; names
        # and busy state for Codex threads need the server.
        click.echo(f"Warning: {exc.message}", err=True)
        threads = []
    return threads, join(threads), terminals


# --- commands ----------------------------------------------------------------------------------


@click.command("list")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Also list threads that are not open in a terminal.",
)
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def list_sessions(show_all: bool, as_json: bool) -> None:
    """List the Codex and Claude Code sessions open in terminals here.

    A session is listed when its process is alive on this computer: a Claude
    Code session with a reachable inbox socket, or a Codex TUI attached to
    the shared app-server. STATE is busy while a turn runs. STEERABLE says
    whether `threads send` can reach it and how.
    """
    threads, sessions, _terminals = gather()
    if as_json:
        payload: dict[str, Any] = {"sessions": [item.to_json() for item in sessions]}
        if show_all:
            terminal_ids = {item.thread_id for item in sessions if item.thread_id}
            payload["threads"] = [
                row for row in threads if row.get("thread_id") not in terminal_ids
            ]
        _json_echo(payload)
        return
    if not sessions and not show_all:
        click.echo(
            "No terminal sessions. Start one with `openbase-coder codex` or "
            "`openbase-coder claude`; `--all` lists every thread."
        )
        return
    rows = [
        (
            item.name,
            item.backend,
            item.state,
            "yes" if item.steerable else "no",
            _home_relative(item.cwd),
            item.reason,
        )
        for item in sessions
    ]
    if show_all:
        terminal_ids = {item.thread_id for item in sessions if item.thread_id}
        rows += [
            (
                thread_name(row) or str(row.get("thread_id")),
                str(row.get("backend") or ""),
                "busy" if thread_is_busy(row) else "idle",
                "via Openbase",
                _home_relative(str(row.get("directory") or "")),
                "not open in a terminal",
            )
            for row in threads
            if row.get("thread_id") not in terminal_ids
        ]
    _print_table(("NAME", "BACKEND", "STATE", "STEERABLE", "FOLDER", "HOW"), rows)


def _print_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    widths = [len(title) for title in header]
    for row in rows:
        for index, cell in enumerate(row[:-1]):
            widths[index] = max(widths[index], len(cell))
    lines = [header, *rows]
    for line in lines:
        cells = [cell.ljust(widths[index]) for index, cell in enumerate(line[:-1])]
        click.echo("  ".join([*cells, line[-1]]).rstrip())


def _message_text(message: str | None) -> str:
    if message is None or message == "-":
        if sys.stdin.isatty():
            raise click.UsageError(
                "Give the message as an argument, or pipe it on stdin."
            )
        message = sys.stdin.read()
    text = message.strip()
    if not text:
        raise click.UsageError("The message is empty.")
    return text


@click.command("send")
@click.argument("session")
@click.argument("message", required=False)
@click.option(
    "--wait",
    is_flag=True,
    help="Wait for the turn to finish and print the agent's reply.",
)
@click.option(
    "--timeout",
    default=DEFAULT_WAIT_SECONDS,
    show_default=True,
    type=float,
    help="With --wait: give up after this many seconds.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the raw result.")
def send(
    session: str, message: str | None, wait: bool, timeout: float, as_json: bool
) -> None:
    """Send MESSAGE to a session by name or thread id.

    SESSION is a name from `threads list` (a unique part of it is enough), a
    thread id, or a Claude Code session id. MESSAGE comes from the argument
    or, when omitted or `-`, from stdin. An idle session starts a new turn
    with it; a busy session reads it as steering during the current turn.
    A foreign Claude inbox only confirms submission, not resumed work.
    """
    text = _message_text(message)
    threads, sessions, terminals = gather()
    target = resolve_target(session, sessions, threads, terminals)
    if target.session is not None and not target.session.steerable:
        raise click.ClickException(
            f"{target.name} cannot be messaged yet: {target.session.reason}."
        )
    sent_at = time.time()
    if target.inbox is not None:
        result = send_via_inbox(target.inbox, text)
        how = "its Claude Code inbox"
    else:
        thread_id = target.thread_id
        if thread_id is None:
            raise click.ClickException(f"{target.name} has no thread to send to.")
        result = send_via_server(thread_id, text, busy=target.busy)
        how = "Openbase"
    result = {**result, "thread_id": target.thread_id, "name": target.name}
    if as_json and not wait:
        _json_echo(result)
        return
    if not as_json:
        verb = {"steer": "steered", "start": "sent to"}.get(
            str(result.get("delivery")), "delivered to"
        )
        if result.get("delivery") in {"unavailable", "inbox_unavailable"}:
            click.echo(str(result.get("message") or "Nothing was delivered or queued; the owner is unavailable."))
        elif result.get("delivery") == "inbox" and not result.get("confirmed"):
            click.echo(f"Message submitted to {target.name} via {how}; delivery is unconfirmed. Do not blindly retry.")
        else:
            click.echo(f"Message {verb} {target.name} via {how}.")
        if result.get("delivery") == "inbox" and target.backend == CLAUDE_BACKEND:
            click.echo(
                "A session running with permissions bypassed may hold it until "
                "approved in that terminal (crossSessionInbound).",
                err=True,
            )
    if not wait:
        return
    if result.get("confirmed") is False or result.get("delivery") in {"inbox", "unavailable", "inbox_unavailable"}:
        raise click.ClickException(
            "Cannot wait for confirmed work from this receipt: delivery is unconfirmed or unavailable. "
            "Inspect the thread before considering a retry."
        )
    thread_id = target.thread_id
    if thread_id is None:
        raise click.ClickException(
            "Cannot wait: Openbase does not list this session yet, so its reply "
            "is only visible in the terminal."
        )
    reply = wait_for_reply(thread_id, since=sent_at - 5.0, timeout=timeout)
    if as_json:
        _json_echo({**result, "reply": reply})
        return
    output = str(reply.get("accumulated_output") or "").strip()
    click.echo(output or "(the turn finished without a text reply)")
