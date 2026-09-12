"""Diagnose why an in-flight Super Agents turn has stalled.

Born from field-test finding FT-9 (2026-09-12): a dispatcher turn ran
``mkdir ~/Desktop/...`` on a fresh install and hung for 6.5 minutes on the
macOS TCC consent dialog ("python3.12 would like to access files in your
Desktop folder") with no timeout, no error, and nothing surfaced to the
phone. The caller (see ``super_agents_client``) races the turn wait against
this module's diagnosis and speaks an actionable hint when a blocking GUI
dialog is the likely cause.

Detection strategy, in order of strength:

1. **Dialog-presenter process recency.** macOS TCC consent dialogs are
   rendered by ``UserNotificationCenter``; authentication sheets by
   ``SecurityAgent``. Both are spawned on demand, so a presenter process
   whose *start time falls inside the current turn* is strong evidence a
   consent dialog appeared while the turn was running. (The process lingers
   after dismissal, so recency — not mere existence — is the signal; and a
   dismissed dialog unblocks the turn anyway, ending the stall.) This needs
   no TCC permission of its own: it is plain ``pgrep``/``ps``.
2. **In-flight tool call.** The Super Agents session log
   (``~/.local/share/super-agents-*/logs/<thread>.log``) records tool_use
   entries as JSON lines; the last Bash/tool invocation without a
   subsequent result is what the turn is stuck on, and names the command in
   the spoken hint.

A window-server probe (CGWindowList owner scan) would be higher precision
for "a dialog is on screen right now"; it is intentionally left as a
follow-up — this module's interface (``diagnose``) is where it would slot
in.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Processes macOS spawns to present consent/auth dialogs.
DIALOG_PRESENTERS = ("UserNotificationCenter", "SecurityAgent")

_PS_TIME_FORMAT = "%a %b %d %H:%M:%S %Y"  # `ps -o lstart=` output


@dataclass
class StallDiagnosis:
    elapsed_seconds: float
    blocking_dialog_process: str | None
    in_flight_tool: str | None
    in_flight_command: str | None

    @property
    def likely_blocked_on_dialog(self) -> bool:
        return self.blocking_dialog_process is not None

    def spoken_hint(self) -> str:
        minutes = max(1, round(self.elapsed_seconds / 60))
        doing = ""
        if self.in_flight_command:
            doing = f" while running {_speakable_command(self.in_flight_command)}"
        elif self.in_flight_tool:
            doing = f" while using {self.in_flight_tool}"
        if self.likely_blocked_on_dialog:
            return (
                f"Heads up — your agent has been waiting{doing} for about "
                f"{minutes} minute{'s' if minutes != 1 else ''}, and a "
                "permission dialog appears to be open on your computer. It "
                "may need you to click Allow."
            )
        return (
            f"Your agent is still working{doing} — about "
            f"{minutes} minute{'s' if minutes != 1 else ''} so far."
        )

    def packet_payload(self) -> dict:
        """Forward-compatible payload for a voice-lifecycle packet so the
        mobile apps can render this visually (planned follow-up)."""
        return {
            "event": "turn_stall_diagnosed",
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "blocking_dialog_process": self.blocking_dialog_process,
            "in_flight_tool": self.in_flight_tool,
            "in_flight_command_excerpt": (self.in_flight_command or "")[:120],
        }


def _speakable_command(command: str) -> str:
    """First token of the command, cleaned for TTS."""
    token = command.strip().split()[0] if command.strip() else "a command"
    return f"the {Path(token).name} command"


def dialog_presenter_started_after(since: _dt.datetime) -> str | None:
    """Name of a dialog-presenter process that started after ``since``.

    A TCC/auth dialog appearing mid-turn spawns its presenter; a presenter
    younger than the turn is therefore a strong blocked-on-dialog signal.
    """
    for name in DIALOG_PRESENTERS:
        try:
            pids = subprocess.run(
                ["pgrep", "-x", name],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.split()
            for pid in pids:
                lstart = subprocess.run(
                    ["ps", "-o", "lstart=", "-p", pid],
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.strip()
                if not lstart:
                    continue
                started = _dt.datetime.strptime(lstart, _PS_TIME_FORMAT)
                if started >= since:
                    return name
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return None


def _session_log_path(thread_id: str) -> Path | None:
    base = Path.home() / ".local" / "share"
    try:
        candidates = sorted(base.glob(f"super-agents-*/logs/{thread_id}.log"))
    except OSError:
        return None
    return candidates[-1] if candidates else None


def in_flight_tool_call(thread_id: str) -> tuple[str | None, str | None]:
    """(tool_name, command_excerpt) of the last unresolved tool call.

    The session log appends one JSON object per line; a ``tool_use`` content
    block without a later ``tool_result`` for the same id is still running.
    """
    path = _session_log_path(thread_id)
    if path is None:
        return None, None
    try:
        # The log can be large; the unresolved call is near the end.
        tail = path.read_bytes()[-65536:].decode("utf-8", errors="replace")
    except OSError:
        return None, None
    pending: dict[str, tuple[str, str]] = {}
    for line in tail.splitlines():
        brace = line.find("{")
        if brace < 0:
            continue
        try:
            entry = json.loads(line[brace:])
        except json.JSONDecodeError:
            continue
        for block in entry.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("name") and block.get("id"):
                command = ""
                if isinstance(block.get("input"), dict):
                    command = str(block["input"].get("command", ""))
                pending[block["id"]] = (block["name"], command)
            if block.get("tool_use_id"):
                pending.pop(block["tool_use_id"], None)
    if not pending:
        return None, None
    name, command = next(reversed(pending.values()))
    return name, (command or None)


def diagnose(
    *,
    turn_started_at: _dt.datetime,
    elapsed_seconds: float,
    thread_id: str,
) -> StallDiagnosis:
    tool, command = in_flight_tool_call(thread_id)
    return StallDiagnosis(
        elapsed_seconds=elapsed_seconds,
        blocking_dialog_process=dialog_presenter_started_after(turn_started_at),
        in_flight_tool=tool,
        in_flight_command=command,
    )


_SQLITE_TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
)


def _parse_sqlite_ts(value: str) -> _dt.datetime | None:
    value = (value or "").replace("Z", "+00:00")
    for fmt in _SQLITE_TS_FORMATS:
        try:
            parsed = _dt.datetime.strptime(value, fmt)
            return parsed.astimezone().replace(tzinfo=None)
        except ValueError:
            continue
    return None


# A spawned agent process blocked at startup on a macOS permission dialog
# (TCC folder access, auth sheet) has its control plane time out during
# initialization, so the turn ends with this signature rather than a
# filesystem error. Confirmed twice in the 2026-09-12 field test (agents
# "Grace" on Documents and "Mary" on Downloads). This is the reliable,
# reproducible signal — unlike racing the transient dialog or the long-lived,
# reused UserNotificationCenter presenter process, both of which live testing
# showed to be unreliable.
CONTROL_INIT_TIMEOUT_SIGNATURE = "control request timeout: initialize"


@dataclass
class BlockedTurn:
    session_id: str
    session_name: str
    agent_name: str | None
    turn_id: str
    last_error: str

    def spoken_hint(self) -> str:
        who = self.agent_name if self.agent_name else "an agent"
        if who.lower() == "dispatcher":
            who = "an agent"
        return (
            f"Heads up — {who} couldn't start on your computer. It may be "
            "showing a permission dialog that needs your approval; check your "
            "Mac and click Allow, then ask me to try again."
        )

    def packet_payload(self) -> dict:
        """Forward-compatible payload for a voice-lifecycle packet so the
        mobile apps can render this visually (planned follow-up)."""
        return {
            "event": "agent_turn_blocked",
            "session_name": self.session_name,
            "agent_name": self.agent_name,
            "turn_id": self.turn_id,
            "reason": "likely_permission_dialog",
        }


def scan_blocked_turns(
    *,
    since: _dt.datetime | None = None,
    now: _dt.datetime | None = None,
    state_db_path: Path | None = None,
) -> list[BlockedTurn]:
    """Find agent turns that recently failed with the blocked-at-init signature.

    Covers the dispatcher AND fire-and-forgotten sub-agents uniformly, since
    both are rows in the shared Super Agents store. ``since`` bounds recency so
    only fresh failures (this call) are surfaced; callers additionally dedupe
    by ``turn_id``.
    """
    import sqlite3

    if state_db_path is None:
        try:
            from openbase_coder_cli.thread_sync.thread_sync_common import (
                super_agents_state_db_path,
            )

            state_db_path = super_agents_state_db_path()
        except Exception:  # noqa: BLE001 - resolution is best-effort
            return []
    if not Path(state_db_path).exists():
        return []
    now = now or _dt.datetime.now()
    try:
        conn = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT t.id AS turn_id, t.last_error, "
            "COALESCE(t.finished_at, t.updated_at) AS ended_at, "
            "s.id AS session_id, s.name, s.agent_name "
            "FROM turns t JOIN sessions s ON s.id = t.session_id "
            "WHERE t.status = 'failed' AND t.last_error IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    results: list[BlockedTurn] = []
    for row in rows:
        error = row["last_error"] or ""
        if CONTROL_INIT_TIMEOUT_SIGNATURE not in error.lower():
            continue
        if since is not None:
            ended = _parse_sqlite_ts(row["ended_at"])
            if ended is not None and ended < since:
                continue
        results.append(
            BlockedTurn(
                session_id=row["session_id"],
                session_name=row["name"],
                agent_name=row["agent_name"],
                turn_id=row["turn_id"],
                last_error=error,
            )
        )
    return results


async def stall_watch_loop(
    *,
    poll_seconds: float = 15.0,
    is_call_active=None,
) -> None:
    """Background poller: surface blocked agent turns during a live call.

    Runs for the lifetime of a LiveKit voice session. Every ``poll_seconds``
    it looks for agent turns (dispatcher OR spawned sub-agent) that just failed
    with the blocked-at-init signature — the reliable signal that a process is
    stuck behind a macOS permission dialog on the user's computer (field-test
    finding FT-9). Each such turn is announced once, so the person on the phone
    learns their Mac needs attention. ``is_call_active``, if given, gates
    speaking so hints never play outside an active call.
    """
    import asyncio

    spoken_turn_ids: set[str] = set()
    loop = asyncio.get_running_loop()
    # Only surface failures observed from when the watcher started, so a
    # pre-existing stale failure doesn't get announced on a fresh call.
    since = _dt.datetime.now()
    while True:
        try:
            await asyncio.sleep(poll_seconds)
            blocked = await loop.run_in_executor(
                None, lambda: scan_blocked_turns(since=since)
            )
            for turn in blocked:
                if turn.turn_id in spoken_turn_ids:
                    continue
                spoken_turn_ids.add(turn.turn_id)
                if is_call_active is not None and not is_call_active():
                    continue
                delivered = await loop.run_in_executor(
                    None, speak_via_local_api, turn.spoken_hint()
                )
                logger.info(
                    "%s stage=agent_turn_blocked_hint_spoken session=%s "
                    "agent=%s turn_id=%s delivered=%s",
                    "dispatch_timing",
                    turn.session_name,
                    turn.agent_name,
                    turn.turn_id,
                    delivered,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a diagnostic must never crash the call
            logger.exception("stall_watch_loop iteration failed")


def speak_via_local_api(text: str, *, agent_name: str = "Dispatcher") -> bool:
    """Speak ``text`` through the local ``user say`` endpoint.

    Reuses the product's existing announcer path so the hint plays inside
    the live call with the standard announcer voice.
    """
    token_path = Path.home() / ".openbase" / "local-api-token"
    try:
        token = token_path.read_text().strip()
    except OSError:
        logger.warning("stall hint not spoken: local API token unavailable")
        return False
    request = urllib.request.Request(
        "http://127.0.0.1:7999/api/user/say/",
        data=json.dumps({"agent_name": agent_name, "text": text}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except OSError as exc:
        logger.warning("stall hint not spoken: %s", exc)
        return False
