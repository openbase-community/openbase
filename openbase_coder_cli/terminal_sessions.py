"""Live terminal sessions: the Codex and Claude Code TUIs open on this computer.

``openbase-coder threads list`` and ``threads send`` work on *terminal
sessions*: conversations a person has open in a terminal (started with
``openbase-coder codex`` / ``openbase-coder claude``, or plain ``codex`` when it
attached to the shared app-server). Nothing below the CLI lists those as such,
so this module derives them from the facts each backend leaves behind:

* **Claude Code** has no shared daemon. A session is reachable only through
  its per-session inbox socket, whose coordinates the Openbase SessionStart
  hook records in the inbox registry (``super_agents.claude_inbox``). A
  registry record whose socket still accepts connections is a live terminal
  session; its transcript tail says whether a turn is in progress.
* **Codex** TUIs attached to the shared app-server own ordinary threads there,
  indistinguishable from dispatched ones (the server stamps every thread with
  its own ``source``/``originator``). A live ``codex`` TUI process is the
  evidence: its working directory and start time (plus the thread id when it
  resumed one) pick its thread out of the threads Openbase lists, after
  excluding threads Super Agents itself started (its state file).

Everything here is pure over injected inputs (thread rows, process listings,
registry records, a socket probe) so it is testable with fakes; the CLI wires
in the real sources.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from super_agents.claude_home_index import latest_custom_title
from super_agents.claude_inbox import InboxRecord, inbox_registry_dir, resolve_inbox

from openbase_coder_cli.agent_launch import codex_session_kind, unix_socket_accepts

CODEX_BACKEND = "codex"
CLAUDE_BACKEND = "claude_code"
BUSY_THREAD_STATUSES = frozenset({"running", "waiting"})

# Codex CLI options that consume the next argv token, so a positional scan
# does not mistake their values for a subcommand or prompt.
_CODEX_VALUE_OPTIONS = frozenset(
    {
        "-p",
        "--profile",
        "-m",
        "--model",
        "-C",
        "--cd",
        "-c",
        "--config",
        "-s",
        "--sandbox",
        "-a",
        "--ask-for-approval",
        "-i",
        "--image",
        "--remote",
        "--remote-auth-token-env",
        "--enable",
        "--disable",
        "--local-provider",
    }
)
_THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
# A TUI registers its thread on the server as it starts; allow for clock
# granularity between `ps` elapsed time and the server's timestamps.
_START_SLACK_SECONDS = 15.0
# A thread whose Super Agents record was created this close to the thread
# itself was started by Super Agents (dispatched), not by a terminal; a
# record created much later only means Super Agents sent to the thread.
_TRACKED_START_SLACK_SECONDS = 30.0
# Bytes read from the end of a Claude transcript to find the last message;
# the window doubles up to the cap when the tail holds only metadata
# (attachments and snapshots can be hundreds of KB).
_TRANSCRIPT_TAIL_BYTES = 64 * 1024
_TRANSCRIPT_TAIL_MAX_BYTES = 4 * 1024 * 1024


# --- processes -------------------------------------------------------------


@dataclass(frozen=True)
class TerminalProcess:
    """A live Codex TUI process."""

    pid: int
    started_at: float
    cwd: str | None
    thread_id: str | None = None
    attached: bool = False


def parse_ps_elapsed(value: str) -> float:
    """Seconds from a ``ps`` ``etime`` value (``[[dd-]hh:]mm:ss``)."""
    days = 0
    rest = value.strip()
    if "-" in rest:
        day_text, rest = rest.split("-", 1)
        days = int(day_text)
    parts = [int(part) for part in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_ps_processes(
    output: str, *, now: float
) -> list[tuple[int, float, list[str]]]:
    """``(pid, started_at, argv)`` per line of ``ps -axo pid=,etime=,args=``."""
    processes: list[tuple[int, float, list[str]]] = []
    for line in output.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        try:
            pid = int(fields[0])
            elapsed = parse_ps_elapsed(fields[1])
        except ValueError:
            continue
        processes.append((pid, now - elapsed, fields[2].split()))
    return processes


def parse_lsof_cwds(output: str) -> dict[int, str]:
    """``{pid: cwd}`` from ``lsof -a -d cwd -Fpn`` field output."""
    cwds: dict[int, str] = {}
    pid: int | None = None
    for line in output.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid is not None:
            cwds[pid] = line[1:]
    return cwds


def _codex_positionals(options: Sequence[str]) -> tuple[list[str], str | None, bool]:
    """Positional args, the ``-C`` directory and whether ``--remote`` is set."""
    positionals: list[str] = []
    cwd: str | None = None
    attached = False
    skip_value = False
    for token in options:
        if skip_value:
            skip_value = False
            continue
        if token == "--":
            continue
        if token.startswith("--remote=") or token == "--remote":
            attached = True
        if token.startswith("-"):
            flag, _, inline_value = token.partition("=")
            if flag in ("-C", "--cd") and inline_value:
                cwd = inline_value
            elif flag in _CODEX_VALUE_OPTIONS and not inline_value:
                skip_value = True
            continue
        positionals.append(token)
    # A separate scan for `-C <dir>`: the value token was skipped above.
    for index, token in enumerate(options):
        if token in ("-C", "--cd") and index + 1 < len(options):
            cwd = options[index + 1]
    return positionals, cwd, attached


def codex_tui_processes(
    ps_output: str, lsof_output: str, *, now: float
) -> list[TerminalProcess]:
    """Live Codex TUI processes from ``ps`` and ``lsof`` listings.

    Only the native ``codex`` binary counts (its ``node`` launcher wrapper has
    a different command name), and only invocations that open the TUI: a new
    session, ``resume`` or ``fork``. Non-interactive subcommands
    (``exec``, ``app-server``, ``mcp-server``, …) are skipped.
    """
    cwds = parse_lsof_cwds(lsof_output)
    processes: list[TerminalProcess] = []
    for pid, started_at, argv in parse_ps_processes(ps_output, now=now):
        if not argv or Path(argv[0]).name != "codex":
            continue
        positionals, cwd_option, attached = _codex_positionals(argv[1:])
        kind = codex_session_kind(positionals)
        if kind == "other":
            continue
        thread_id = None
        if kind == "tui-subcommand" and len(positionals) > 1:
            candidate = positionals[1]
            if _THREAD_ID_RE.match(candidate):
                thread_id = candidate
        processes.append(
            TerminalProcess(
                pid=pid,
                started_at=started_at,
                cwd=cwd_option or cwds.get(pid),
                thread_id=thread_id,
                attached=attached,
            )
        )
    return processes


def live_codex_tui_processes(*, now: float) -> list[TerminalProcess]:
    """Scan this computer for Codex TUI processes."""
    ps_output = _run(["ps", "-axo", "pid=,etime=,args="])
    lsof_output = _run(["lsof", "-a", "-c", "codex", "-d", "cwd", "-Fpn"])
    return codex_tui_processes(ps_output, lsof_output, now=now)


def _run(argv: list[str]) -> str:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout


# --- Claude Code inbox records -------------------------------------------------


@dataclass(frozen=True)
class ClaudeTerminal:
    """A Claude Code session with a recorded inbox socket."""

    record: InboxRecord
    live: bool
    busy: bool | None = None
    # The session's own name (`claude --name`, `/rename`), from its transcript.
    title: str | None = None


def claude_inbox_records(registry_dir: Path | None = None) -> list[InboxRecord]:
    """Every record in the inbox registry (the SessionStart hook writes them)."""
    directory = registry_dir or inbox_registry_dir()
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return []
    records: list[InboxRecord] = []
    for path in paths:
        record = resolve_inbox(path.stem, registry_dir=directory)
        if record is not None:
            records.append(record)
    return records


def claude_terminals(
    records: Iterable[InboxRecord],
    *,
    socket_accepts: Callable[[Path], bool] = unix_socket_accepts,
    transcript_for: Callable[[InboxRecord], Path | None],
    title_for: Callable[[Path], str | None] = latest_custom_title,
) -> list[ClaudeTerminal]:
    """Probe each record's socket; a connectable one is a live session."""
    terminals: list[ClaudeTerminal] = []
    for record in records:
        live = record.path_exists and socket_accepts(Path(record.socket))
        busy = title = None
        if live:
            transcript = transcript_for(record)
            if transcript is not None:
                busy = claude_transcript_busy(transcript)
                title = title_for(transcript)
        terminals.append(
            ClaudeTerminal(record=record, live=live, busy=busy, title=title)
        )
    return terminals


# Text of user entries a person typed that run locally and start no turn
# (slash commands such as /model, and their echoed output).
_LOCAL_COMMAND_PREFIXES = ("<command-name>", "<local-command", "<command-message>")


def _starts_turn(entry: dict[str, Any]) -> bool:
    """Whether a ``user`` transcript entry hands the model work to do."""
    if entry.get("isMeta"):
        return False
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return not content.lstrip().startswith(_LOCAL_COMMAND_PREFIXES)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return True
            text = block.get("text")
            if isinstance(text, str) and not text.lstrip().startswith(
                _LOCAL_COMMAND_PREFIXES
            ):
                return True
    return False


def claude_transcript_busy(path: Path) -> bool | None:
    """Whether the transcript's last message leaves a turn in progress.

    Claude Code appends every message as it goes: a trailing user message
    (prompt or tool result) or an assistant message that stopped to call a
    tool means the model still has work to do; an assistant message that
    ended its turn means the session is waiting at the prompt. User entries
    that start no turn (local slash commands, meta entries) are skipped.
    None when the tail holds no message at all (a brand-new session).
    """
    window = _TRANSCRIPT_TAIL_BYTES
    while True:
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - window))
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        verdict = _busy_from_tail(tail)
        if (
            verdict is not None
            or window >= size
            or window >= _TRANSCRIPT_TAIL_MAX_BYTES
        ):
            return verdict
        window *= 2


def _busy_from_tail(tail: str) -> bool | None:
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind == "user":
            if _starts_turn(entry):
                return True
            continue
        if kind == "assistant":
            message = entry.get("message")
            stop_reason = (
                message.get("stop_reason") if isinstance(message, dict) else None
            )
            return stop_reason == "tool_use"
    return None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind == "user":
            if _starts_turn(entry):
                return True
            continue
        if kind == "assistant":
            message = entry.get("message")
            stop_reason = (
                message.get("stop_reason") if isinstance(message, dict) else None
            )
            return stop_reason == "tool_use"
    return None


# --- joining threads with terminals ---------------------------------------------


@dataclass(frozen=True)
class TerminalSession:
    """One terminal session as ``threads list`` shows it."""

    backend: str
    name: str
    cwd: str
    thread_id: str | None
    steerable: bool
    reason: str
    busy: bool | None = None
    pid: int | None = None
    backend_session_id: str | None = None

    @property
    def state(self) -> str:
        if self.busy is None:
            return "unknown"
        return "busy" if self.busy else "idle"

    def to_json(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "name": self.name,
            "cwd": self.cwd,
            "thread_id": self.thread_id,
            "backend_session_id": self.backend_session_id,
            "state": self.state,
            "steerable": self.steerable,
            "reason": self.reason,
            "pid": self.pid,
        }


def thread_name(row: dict[str, Any]) -> str | None:
    for key in ("name", "display_name", "title"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def thread_is_busy(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "") in BUSY_THREAD_STATUSES


def iso_epoch(value: Any) -> float | None:
    """Epoch seconds of an ISO-8601 timestamp (naive means UTC), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def row_time(row: dict[str, Any], key: str) -> float | None:
    """Epoch seconds of a row's ISO timestamp field, or None."""
    return iso_epoch(row.get(key))


def _claude_sessions(
    threads: Sequence[dict[str, Any]], terminals: Iterable[ClaudeTerminal]
) -> list[TerminalSession]:
    rows_by_session = {
        str(row.get("backend_session_id")): row
        for row in threads
        if row.get("backend") == CLAUDE_BACKEND and row.get("backend_session_id")
    }
    sessions: list[TerminalSession] = []
    for terminal in terminals:
        if not terminal.live:
            continue
        record = terminal.record
        row = rows_by_session.get(record.session_id)
        if row is None:
            sessions.append(
                TerminalSession(
                    backend=CLAUDE_BACKEND,
                    name=terminal.title or f"claude {record.session_id[:8]}",
                    cwd=record.cwd or "",
                    thread_id=None,
                    steerable=True,
                    reason=(
                        "Claude Code inbox socket; Openbase lists it once a "
                        "message is typed in it"
                    ),
                    busy=terminal.busy,
                    backend_session_id=record.session_id,
                )
            )
            continue
        sessions.append(
            TerminalSession(
                backend=CLAUDE_BACKEND,
                name=thread_name(row) or f"claude {record.session_id[:8]}",
                cwd=str(row.get("directory") or record.cwd or ""),
                thread_id=str(row.get("thread_id")),
                steerable=True,
                reason="Claude Code inbox socket",
                busy=terminal.busy,
                backend_session_id=record.session_id,
            )
        )
    return sessions


def started_by_super_agents(
    row: dict[str, Any], tracked_starts: dict[str, float]
) -> bool:
    """Whether Super Agents created this thread (rather than sending to it later)."""
    tracked_at = tracked_starts.get(str(row.get("thread_id")))
    if tracked_at is None:
        return False
    created_at = row_time(row, "created_at")
    if created_at is None:
        return True
    return abs(tracked_at - created_at) <= _TRACKED_START_SLACK_SECONDS


def _codex_thread_for_process(
    process: TerminalProcess,
    rows: Sequence[dict[str, Any]],
    tracked_starts: dict[str, float],
    claimed: set[str],
) -> dict[str, Any] | None:
    if process.thread_id:
        for row in rows:
            if row.get("thread_id") == process.thread_id:
                return row
        return None
    if not process.cwd:
        return None
    in_cwd = [
        row
        for row in rows
        if row.get("directory") == process.cwd
        and str(row.get("thread_id")) not in claimed
        and not started_by_super_agents(row, tracked_starts)
    ]
    threshold = process.started_at - _START_SLACK_SECONDS
    started_here = [
        row for row in in_cwd if (row_time(row, "created_at") or 0) >= threshold
    ]
    if started_here:
        # A /new inside the TUI makes a newer thread; that one is on screen.
        return max(started_here, key=lambda row: row_time(row, "created_at") or 0)
    # `resume --last` or the picker: an older thread touched since the TUI
    # started.
    touched = [row for row in in_cwd if (row_time(row, "updated_at") or 0) >= threshold]
    if touched:
        return max(touched, key=lambda row: row_time(row, "updated_at") or 0)
    return None


def _codex_sessions(
    threads: Sequence[dict[str, Any]],
    processes: Iterable[TerminalProcess],
    tracked_starts: dict[str, float],
) -> list[TerminalSession]:
    rows = [row for row in threads if row.get("backend") == CODEX_BACKEND]
    sessions: list[TerminalSession] = []
    claimed: set[str] = set()
    for process in sorted(processes, key=lambda item: item.started_at, reverse=True):
        row = _codex_thread_for_process(process, rows, tracked_starts, claimed)
        if row is None:
            sessions.append(
                TerminalSession(
                    backend=CODEX_BACKEND,
                    name="codex (no messages yet)",
                    cwd=process.cwd or "",
                    thread_id=None,
                    steerable=False,
                    reason=(
                        "Openbase lists a Codex session after its first "
                        "message; send one in the terminal first"
                        if process.attached or process.cwd
                        else "not attached to the shared Codex app-server"
                    ),
                    pid=process.pid,
                )
            )
            continue
        thread_id = str(row.get("thread_id"))
        claimed.add(thread_id)
        sessions.append(
            TerminalSession(
                backend=CODEX_BACKEND,
                name=thread_name(row) or f"codex {thread_id[:8]}",
                cwd=str(row.get("directory") or process.cwd or ""),
                thread_id=thread_id,
                steerable=True,
                reason="shared Codex app-server",
                busy=thread_is_busy(row),
                pid=process.pid,
            )
        )
    return sessions


def terminal_sessions(
    threads: Sequence[dict[str, Any]],
    *,
    codex_processes: Iterable[TerminalProcess],
    claude_terminals: Iterable[ClaudeTerminal],
    tracked_starts: dict[str, float],
) -> list[TerminalSession]:
    """Join Openbase's thread rows with the live terminals on this computer.

    ``tracked_starts`` maps thread ids Super Agents tracks to the time it
    created their record (``threads_terminal.tracked_thread_starts``).
    """
    sessions = _claude_sessions(threads, claude_terminals)
    sessions += _codex_sessions(threads, codex_processes, tracked_starts)
    return sorted(sessions, key=lambda item: (item.cwd, item.name.lower()))
