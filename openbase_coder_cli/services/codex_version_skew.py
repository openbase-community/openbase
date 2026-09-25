"""Detect Codex app-server services running an older Codex than is installed.

The managed ``codex-app-server`` services are long-lived launchd processes.
When Codex is upgraded underneath them (``npm install -g``, a new nvm node
version with its own global install, or the CLI self-update refreshing the
bundled binary) they keep the old binary in memory until restarted, and
every new Codex CLI launch then warns "A background Codex service is
running vX, older than your Codex CLI vY".

Detection needs no bookkeeping at start time: the running server reports
its version in the ``userAgent`` of the ``initialize`` handshake, and the
installed version is whatever the service resolver would exec today.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

CODEX_APP_SERVER_SERVICE_NAMES: tuple[str, ...] = (
    "codex-app-server",
    "codex-app-server-dispatcher",
)

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
# ``<originator>/<version> (<os>; <arch>) ...`` where the originator is the
# name of the first client that initialized the server, not a fixed token.
_USER_AGENT_VERSION_RE = re.compile(r"^[\w.-]+/(\d+\.\d+\.\d+)")
_HANDSHAKE_TIMEOUT_SECONDS = 5.0

_installed_cache_lock = threading.Lock()
# (path, mtime_ns, size) -> version; the binary is hundreds of MB and
# ``--version`` costs ~100ms, so only re-run it when the file changes.
_installed_cache: tuple[tuple[str, int, int], str | None] | None = None


@dataclass(frozen=True)
class CodexVersionSkew:
    service: str
    running_version: str
    installed_version: str
    installed_path: str

    @property
    def message(self) -> str:
        return (
            f"Service '{self.service}' is running Codex {self.running_version}, "
            f"but Codex {self.installed_version} is installed."
        )


def parse_codex_version(text: str) -> str | None:
    """``codex --version`` prints ``codex-cli X.Y.Z``."""
    match = _VERSION_RE.search(text or "")
    return match.group(1) if match else None


def parse_user_agent_version(user_agent: str | None) -> str | None:
    """Version from the app-server's initialize ``userAgent``."""
    if not isinstance(user_agent, str):
        return None
    match = _USER_AGENT_VERSION_RE.match(user_agent.strip())
    return match.group(1) if match else None


def resolve_installed_codex() -> Path | None:
    """The codex binary a service (re)start would exec right now."""
    from openbase_coder_cli.backend_binaries import find_backend_binary

    try:
        return find_backend_binary("codex")
    except Exception:  # noqa: BLE001 - resolution must never break health
        return None


def installed_codex_version(binary: Path | None = None) -> tuple[str, str] | None:
    """``(path, version)`` of the installed codex, or ``None`` when unknown."""
    global _installed_cache

    resolved = binary or resolve_installed_codex()
    if resolved is None:
        return None
    try:
        stat = resolved.stat()
    except OSError:
        return None
    key = (str(resolved), stat.st_mtime_ns, stat.st_size)
    with _installed_cache_lock:
        cached = _installed_cache
        if cached is not None and cached[0] == key:
            return (key[0], cached[1]) if cached[1] else None
    try:
        result = subprocess.run(
            [str(resolved), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        version = parse_codex_version(result.stdout + result.stderr)
    except (OSError, subprocess.TimeoutExpired):
        version = None
    with _installed_cache_lock:
        _installed_cache = (key, version)
    return (key[0], version) if version else None


async def _running_version_async(endpoint) -> str | None:
    from super_agents.app_endpoint import open_app_server_connection

    connection = await asyncio.wait_for(
        open_app_server_connection(endpoint, open_timeout=_HANDSHAKE_TIMEOUT_SECONDS),
        timeout=_HANDSHAKE_TIMEOUT_SECONDS,
    )
    try:
        await connection.send(
            json.dumps(
                {
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "openbase-coder-version-probe",
                            "title": "Openbase Coder version probe",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                }
            )
        )
        while True:
            raw = await asyncio.wait_for(
                connection.recv(), timeout=_HANDSHAKE_TIMEOUT_SECONDS
            )
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get("id") != 0:
                continue
            if message.get("error"):
                return None
            result = message.get("result")
            user_agent = result.get("userAgent") if isinstance(result, dict) else None
            return parse_user_agent_version(user_agent)
    finally:
        await connection.close()


def running_codex_app_server_version(endpoint) -> str | None:
    """Version reported by the app-server behind ``endpoint``, or ``None``."""
    try:
        return asyncio.run(_running_version_async(endpoint))
    except Exception:  # noqa: BLE001 - an unreachable server is not a skew
        return None


def service_endpoint(service_name: str):
    from openbase_coder_cli.codex_control_plane import (
        dispatcher_codex_app_server_endpoint,
        managed_codex_app_server_endpoint,
    )

    if service_name == "codex-app-server-dispatcher":
        return dispatcher_codex_app_server_endpoint()
    return managed_codex_app_server_endpoint()


def service_version_skew(
    service_name: str, installed: tuple[str, str] | None = None
) -> CodexVersionSkew | None:
    """Skew for one running codex service, or ``None`` when versions agree.

    Also ``None`` when either side is unknown: a service that is down or
    unreachable is reported by the service-status checks instead.
    """
    installed = installed or installed_codex_version()
    if installed is None:
        return None
    running = running_codex_app_server_version(service_endpoint(service_name))
    if running is None or running == installed[1]:
        return None
    return CodexVersionSkew(
        service=service_name,
        running_version=running,
        installed_version=installed[1],
        installed_path=installed[0],
    )


def collect_codex_version_skews() -> list[CodexVersionSkew]:
    """Skews for every installed, running codex service on this machine."""
    from openbase_coder_cli.services.launchd import launchctl_status
    from openbase_coder_cli.services.registry import find_service
    from openbase_coder_cli.services.selection import (
        service_supports_configured_backends,
    )

    installed = installed_codex_version()
    if installed is None:
        return []
    skews: list[CodexVersionSkew] = []
    for name in CODEX_APP_SERVER_SERVICE_NAMES:
        try:
            service = find_service(name)
            if not service_supports_configured_backends(service):
                continue
            info = launchctl_status(service)
        except Exception:  # noqa: BLE001 - status probe must never break health
            continue
        if not info.get("pid"):
            continue
        skew = service_version_skew(name, installed)
        if skew is not None:
            skews.append(skew)
    return skews


# --- idle auto-restart -------------------------------------------------------

AUTO_RESTART_ENV = "OPENBASE_CODEX_AUTO_RESTART"


def auto_restart_enabled() -> bool:
    value = os.environ.get(AUTO_RESTART_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


# A turn with no activity for this long is stale bookkeeping, not work in
# flight: the state file keeps "running" turns from sessions that died
# without a terminal event (observed 2026-09-24: 11 of 23 "active" sessions
# were 3-28 days old).
ACTIVE_TURN_MAX_AGE_SECONDS = 6 * 3600.0
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _iso_to_epoch(value: str | None) -> float:
    from datetime import datetime

    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def super_agents_active_turn_count(
    state_file: Path | None = None,
    *,
    now: float | None = None,
    max_age_seconds: float = ACTIVE_TURN_MAX_AGE_SECONDS,
) -> int | None:
    """Sessions the Super Agents state file tracks as running or waiting.

    A session counts when it has an active turn (or a turn still marked
    running/waiting), its last status is not terminal, and it showed
    activity within ``max_age_seconds``. ``None`` means the file could not
    be read, which callers treat as busy: restarting under an unknown
    workload is the one thing this must not do.
    """
    import time

    from super_agents.app_server_client import DEFAULT_STATE_FILE
    from super_agents.state import read_state_file

    path = (
        state_file
        or Path(
            os.environ.get("SUPER_AGENTS_STATE_FILE") or DEFAULT_STATE_FILE
        ).expanduser()
    )
    if not path.exists():
        return 0
    try:
        # read_state_file() falls back to an empty state on unreadable input,
        # which would look idle; validate the JSON first so corruption
        # counts as unknown instead.
        json.loads(path.read_text(encoding="utf-8"))
        state = read_state_file(path)
    except Exception:  # noqa: BLE001
        return None
    current = time.time() if now is None else now
    active = 0
    for session in state.sessions.values():
        if session.last_status in _TERMINAL_STATUSES and not session.active_turn_id:
            continue
        tracked = bool(session.active_turn_id) or any(
            turn.status in {"running", "waiting"}
            for turn in (session.turns or {}).values()
        )
        if not tracked:
            continue
        last_activity = max(
            _iso_to_epoch(session.last_event_at), _iso_to_epoch(session.updated_at)
        )
        if current - last_activity > max_age_seconds:
            continue
        active += 1
    return active


def voice_session_active() -> bool | None:
    """``None`` when indeterminate (LiveKit down or no credentials)."""
    from openbase_coder_cli.livekit_announcer import active_voice_room_exists

    try:
        return asyncio.run(active_voice_room_exists(include_agent_only_rooms=True))
    except Exception:  # noqa: BLE001
        return None


def restart_blockers(
    *,
    active_turns: int | None,
    voice_active: bool | None,
) -> list[str]:
    """Why an automatic restart must wait; empty means it is safe now."""
    blockers: list[str] = []
    if active_turns is None:
        blockers.append("agent activity unknown")
    elif active_turns:
        blockers.append(f"{active_turns} active agent turn(s)")
    if voice_active is None:
        blockers.append("voice session state unknown")
    elif voice_active:
        blockers.append("voice session in progress")
    return blockers


# Once a restart was scheduled for a (running, installed) pair, do not
# reschedule for the same pair: if the restart did not resolve it, something
# is wrong (a resolver disagreement, a failing start) and a restart loop would
# only make it worse. The banner keeps showing the skew for a manual fix.
_last_scheduled: dict[str, tuple[str, str]] = {}
_last_scheduled_lock = threading.Lock()


def run_auto_restart_tick() -> dict[str, object]:
    """Restart skewed codex services when nothing is in flight.

    Returns a summary for logging/tests: ``skews`` found, ``blockers`` (why
    the restart waited), and ``restarted`` (service names scheduled).
    """
    summary: dict[str, object] = {"skews": [], "blockers": [], "restarted": []}
    if not auto_restart_enabled():
        return summary
    skews = collect_codex_version_skews()
    summary["skews"] = [skew.service for skew in skews]
    if not skews:
        with _last_scheduled_lock:
            _last_scheduled.clear()
        return summary

    with _last_scheduled_lock:
        pending = [
            skew
            for skew in skews
            if _last_scheduled.get(skew.service)
            != (skew.running_version, skew.installed_version)
        ]
    if not pending:
        summary["blockers"] = ["already restarted for this version pair"]
        return summary

    blockers = restart_blockers(
        active_turns=super_agents_active_turn_count(),
        voice_active=voice_session_active(),
    )
    summary["blockers"] = blockers
    if blockers:
        return summary

    from openbase_coder_cli.services.restart import RestartRequest, schedule_restart

    names = tuple(skew.service for skew in pending)
    schedule_restart(RestartRequest(services=names), warn=False)
    with _last_scheduled_lock:
        for skew in pending:
            _last_scheduled[skew.service] = (
                skew.running_version,
                skew.installed_version,
            )
    summary["restarted"] = list(names)
    logger.info(
        "codex_version_skew auto_restart services=%s running=%s installed=%s",
        list(names),
        [skew.running_version for skew in pending],
        pending[0].installed_version,
    )
    return summary
