"""Self-heal the livekit-agent's stale pre-warmed job process pool.

The livekit-agent keeps a pool of pre-warmed job processes ready to service
incoming voice calls. After the Mac sleeps/wakes or Tailscale churns, that
pool can go stale: an incoming call logs ``received job request`` but the
dispatched job never completes the WebRTC handshake. The agent log shows
``wait_pc_connection timed out`` (``failed to connect: Connection("wait_pc_
connection timed out")`` then ``process exiting``), the agent never reaches
``dispatch_timing stage=agent_session_start_complete``, and the user's call
dies on "waiting for agent". The documented manual fix is to bounce
livekit-server + livekit-agent.

This module automates that fix and, better, prevents the staleness from ever
accruing. It runs on the ``sync-workers`` service tick and does two things:

- **Branch A — failure-signature watchdog.** Incrementally tails
  ``livekit-agent.log`` and, on seeing ``wait_pc_connection timed out`` in
  newly-written content, bounces the agent. A recurrence within a short
  window escalates to bouncing livekit-server + livekit-agent (the manual
  remedy). Only newly-appended lines are ever considered, so a restart never
  re-reacts to historical failures.
- **Branch B — idle recycling.** When the agent has sat idle long enough
  (default 45 min) with no active call, it proactively recycles the agent so
  a fresh pool replaces the potentially-stale one before the next call lands.

Both branches are guarded so self-heal never harms a live call and never
churns the service: they never bounce while a voice session is active (a
signature that fires mid-call is deferred via a pending flag and bounced once
the call ends), and they share a persistent rolling rate limit. All state
(log read offset, escalation/idle timers, rate-limit history, pending flag)
lives in a single versioned JSON file so the behavior survives restarts.
"""

from __future__ import annotations

import logging
import os
import time

from openbase_coder_cli.paths import DEFAULT_LOG_DIR, OPENBASE_BASE_DIR

logger = logging.getLogger(__name__)

# The failure narrative we react to (substring match on newly-written lines).
STALE_POOL_SIGNATURE = "wait_pc_connection timed out"

# Branch A tick cadence (env override wired into the SyncJob in sync_workers).
WATCHDOG_TICK_SECONDS = 30.0

# A signature recurrence within this window of the previous watchdog failure
# bounce escalates from bouncing the agent to bouncing server + agent.
ESCALATION_WINDOW_SECONDS = 900.0  # 15 min

# Shared rolling rate limit across both branches, persisted across restarts.
RATE_LIMIT_MAX_BOUNCES = 3
RATE_LIMIT_WINDOW_SECONDS = 1800.0  # 30 min

# Branch B: recycle the idle agent after this long with no bounce/recycle/call.
IDLE_RECYCLE_SECONDS = 2700.0  # 45 min

# A deferred (active-call-blocked) failure bounce is forgotten after this long.
PENDING_TTL_SECONDS = 900.0  # 15 min

# Cap each incremental log read so a huge backlog never blocks the tick.
MAX_LOG_READ_BYTES = 2 * 1024 * 1024

STATE_VERSION = 1

_LOG_PATH = DEFAULT_LOG_DIR / "livekit-agent.log"
_STATE_PATH = OPENBASE_BASE_DIR / "livekit-pool-watchdog.json"

_AGENT_SERVICE_NAME = "livekit-agent"
_SERVER_SERVICE_NAME = "livekit-server"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _voice_session_active() -> bool:
    """True while a voice room exists; indeterminate (no creds / livekit down)
    counts as no session. Mirrors self_update._voice_session_active so the
    guard is identical without importing that private helper."""
    import asyncio

    from openbase_coder_cli.livekit_announcer import active_voice_room_exists

    try:
        return asyncio.run(active_voice_room_exists())
    except Exception:
        return False


def _agent_service_running() -> bool:
    """True only when the livekit-agent service is installed and has a pid."""
    from openbase_coder_cli.services.launchd import launchctl_status
    from openbase_coder_cli.services.registry import find_service

    try:
        status = launchctl_status(find_service(_AGENT_SERVICE_NAME))
    except Exception:
        return False
    return bool(status.get("installed")) and bool(status.get("pid"))


def _fresh_state() -> dict:
    return {
        "version": STATE_VERSION,
        "initialized": False,
        "log_offset": 0,
        "baseline_ts": None,
        "last_failure_bounce_ts": None,
        "last_idle_recycle_ts": None,
        "bounce_history": [],
        "pending": None,
    }


def _read_state() -> dict:
    """Load state, tolerating a missing or corrupt file by resetting."""
    import json

    state = _fresh_state()
    try:
        if _STATE_PATH.is_file():
            payload = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                state.update(payload)
    except Exception:
        logger.warning(
            "livekit_pool_watchdog unreadable state; resetting", exc_info=True
        )
        return _fresh_state()
    # Normalize the rate-limit history to a list of floats.
    history = state.get("bounce_history")
    if not isinstance(history, list):
        state["bounce_history"] = []
    else:
        state["bounce_history"] = [t for t in history if isinstance(t, int | float)]
    return state


def _write_state(state: dict) -> None:
    import json

    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    except Exception:
        logger.warning("livekit_pool_watchdog unable to write state", exc_info=True)


def _rate_limit_allow(state: dict, now: float) -> bool:
    """Prune the rolling window and, if under the cap, record and allow a
    bounce. Shared by both branches; persisted in the state dict."""
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    recent = [t for t in state.get("bounce_history", []) if t >= cutoff]
    if len(recent) >= RATE_LIMIT_MAX_BOUNCES:
        state["bounce_history"] = recent
        return False
    state["bounce_history"] = [*recent, now]
    return True


def _execute_bounce(services: tuple[str, ...]) -> None:
    from openbase_coder_cli.services.restart import (
        RestartRequest,
        build_restart_plan,
        execute_restart_plan,
    )

    plan = build_restart_plan(RestartRequest(services=services, delay_seconds=0.0))
    execute_restart_plan(plan)


def _read_new_log_content(state: dict) -> str:
    """Return log bytes appended since the recorded offset (capped), advancing
    the offset. Handle truncation (service restart truncates the log to the
    last 5000 lines) and a missing file by re-seeking to EOF without scanning,
    so we never re-react to the post-restart tail or historical failures."""
    try:
        current_size = _LOG_PATH.stat().st_size if _LOG_PATH.is_file() else 0
    except OSError:
        current_size = 0

    offset = int(state.get("log_offset") or 0)
    if current_size < offset:
        # Truncated or recreated smaller: reset and seek to EOF, do not scan.
        state["log_offset"] = current_size
        return ""
    if current_size <= offset:
        return ""

    read_bytes = min(current_size - offset, MAX_LOG_READ_BYTES)
    try:
        with _LOG_PATH.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(read_bytes)
    except OSError:
        logger.warning("livekit_pool_watchdog unable to read log", exc_info=True)
        return ""
    state["log_offset"] = offset + len(data)
    return data.decode("utf-8", errors="ignore")


def _initialize(state: dict, now: float) -> None:
    """First-ever run: seek to EOF and set the idle baseline without scanning
    any pre-existing content, so we never bounce on historical failures or
    recycle immediately at startup."""
    try:
        current_size = _LOG_PATH.stat().st_size if _LOG_PATH.is_file() else 0
    except OSError:
        current_size = 0
    state["initialized"] = True
    state["log_offset"] = current_size
    state["baseline_ts"] = now


def _pending_active(state: dict, now: float) -> bool:
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return False
    created = pending.get("created_ts")
    if not isinstance(created, int | float):
        return False
    return (now - created) <= PENDING_TTL_SECONDS


def _bounce_failure(state: dict, now: float) -> bool:
    """Fire (or defer) a stale-pool-signature bounce. Returns True if a bounce
    was executed this tick."""
    if _voice_session_active():
        # Defer: remember the intent so the next tick bounces once the call
        # ends. The signature lines are already consumed; the flag carries on.
        state["pending"] = {"created_ts": now}
        logger.info(
            "livekit_pool_watchdog deferred reason=stale_pool_signature "
            "cause=active_voice_session"
        )
        return False

    # We are handling the intent now; clear the pending flag regardless of the
    # outcome so a rate-limited attempt does not retry forever.
    state["pending"] = None

    if not _rate_limit_allow(state, now):
        logger.warning(
            "livekit_pool_watchdog rate_limited reason=stale_pool_signature "
            "max=%d window_s=%.0f",
            RATE_LIMIT_MAX_BOUNCES,
            RATE_LIMIT_WINDOW_SECONDS,
        )
        return False

    last = state.get("last_failure_bounce_ts")
    escalate = (
        isinstance(last, int | float) and (now - last) <= ESCALATION_WINDOW_SECONDS
    )
    if escalate:
        services = (_SERVER_SERVICE_NAME, _AGENT_SERVICE_NAME)
        reason = "stale_pool_signature_escalated"
    else:
        services = (_AGENT_SERVICE_NAME,)
        reason = "stale_pool_signature"

    logger.info("livekit_pool_watchdog bounce reason=%s services=%s", reason, services)
    _execute_bounce(services)
    state["last_failure_bounce_ts"] = now
    return True


def _bounce_idle(state: dict, now: float) -> bool:
    """Proactively recycle the idle agent. Returns True if it bounced."""
    idle_seconds = _env_float(
        "LIVEKIT_AGENT_IDLE_RECYCLE_SECONDS", IDLE_RECYCLE_SECONDS
    )
    if idle_seconds <= 0:
        return False

    baseline = state.get("baseline_ts") or 0.0
    last_failure = state.get("last_failure_bounce_ts") or 0.0
    last_idle = state.get("last_idle_recycle_ts") or 0.0
    last_activity = max(baseline, last_failure, last_idle)
    if (now - last_activity) < idle_seconds:
        return False

    if _voice_session_active():
        return False

    if not _rate_limit_allow(state, now):
        logger.warning(
            "livekit_pool_watchdog rate_limited reason=idle_recycle "
            "max=%d window_s=%.0f",
            RATE_LIMIT_MAX_BOUNCES,
            RATE_LIMIT_WINDOW_SECONDS,
        )
        # Advance the idle timer so we do not re-attempt every tick.
        state["last_idle_recycle_ts"] = now
        return False

    logger.info(
        "livekit_pool_watchdog bounce reason=idle_recycle services=('%s',)",
        _AGENT_SERVICE_NAME,
    )
    _execute_bounce((_AGENT_SERVICE_NAME,))
    # Idle recycles reset the idle timer but do NOT count toward the failure
    # escalation ladder (only last_idle_recycle_ts advances, not the failure
    # bounce timestamp).
    state["last_idle_recycle_ts"] = now
    return True


def run_tick() -> None:
    """One watchdog pass: tail the log for the failure signature, self-heal on
    detection (with an active-call guard, escalation, and rate limit), and
    proactively recycle the idle agent. Delegated to from sync_workers."""
    if not _agent_service_running():
        logger.debug("livekit_pool_watchdog skipped agent_not_running")
        return

    now = time.time()
    state = _read_state()

    if not state.get("initialized"):
        _initialize(state, now)
        _write_state(state)
        return

    new_content = _read_new_log_content(state)
    signature_detected = new_content.count(STALE_POOL_SIGNATURE) > 0

    bounced = False
    if signature_detected or _pending_active(state, now):
        bounced = _bounce_failure(state, now)
    else:
        # A stale/expired pending flag is cleared so it cannot linger.
        if state.get("pending") is not None:
            state["pending"] = None

    if not bounced:
        _bounce_idle(state, now)

    _write_state(state)
