"""Exit the agent when its LiveKit worker is permanently broken.

Two failure modes leave launchd/systemd keeping a useless process alive:

- Job workers that repeatedly fail to initialize (for example after the
  virtualenv was rebuilt for a different Python while the service kept
  running): the agent stays registered with LiveKit while every dispatched
  job dies — calls connect and then silently never get an agent.
- The worker's reconnect loop raising after exhausting its retries (for
  example when livekit-server stays down for a few minutes): the SDK never
  restarts its connection task, so the agent lingers unregistered forever
  and calls wait for an agent that can never arrive.
- AgentSession closing after an unrecoverable in-room pipeline error (for
  example a terminal STT websocket reconnect failure): the room can remain
  connected while no further speech is transcribed.

Exiting is the recovery in these cases: the service manager restarts the
agent, which boots against the current environment and re-registers.
"""

import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

from openbase_coder_cli.paths import OPENBASE_BASE_DIR

logger = logging.getLogger(__name__)

WORKER_INIT_FAILURE_THRESHOLD = 3
WORKER_INIT_FAILURE_WINDOW_SECONDS = 120.0
WORKER_EXIT_RATE_LIMIT_THRESHOLD = 3
WORKER_EXIT_RATE_LIMIT_WINDOW_SECONDS = 300.0
_FORCED_EXIT_GRACE_SECONDS = 15.0
_INIT_FAILURE_MESSAGE = "error initializing process"
# livekit-agents logs this (via @log_exceptions) when _connection_task
# raises — after max_retry failed connect attempts it is terminal: the SDK
# never restarts the task, so the worker can never reconnect.
_CONNECTION_FAILURE_MESSAGE = "Error in _connection_task"
_AGENT_SESSION_UNRECOVERABLE_MESSAGE = (
    "AgentSession is closing due to unrecoverable error"
)
_AGENT_SESSION_UNRECOVERABLE_RATE_LIMIT_KEY = "agent_session_unrecoverable"
_EXIT_RATE_LIMIT_STATE_PATH = OPENBASE_BASE_DIR / "livekit-agent-watchdog-exits.json"


class WorkerFailureWatchdog(logging.Handler):
    """Log handler that exits the service on unrecoverable worker failures."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self._failure_times: list[float] = []
        self._lock = threading.Lock()
        self._exiting = False

    def emit(self, record: logging.LogRecord) -> None:
        message = str(record.getMessage())
        if _CONNECTION_FAILURE_MESSAGE in message:
            self._exit_once(
                "livekit-agent's LiveKit connection task died (the SDK never "
                "restarts it); exiting so the service manager restarts the "
                "agent"
            )
            return
        if _AGENT_SESSION_UNRECOVERABLE_MESSAGE in message:
            # Room-scoped status/lifecycle packets are best-effort from
            # session_diagnostics when AgentSession emits error/close events.
            # This process-global log handler has no room handle; its job is
            # bounded self-heal when the SDK only surfaces the fatal log.
            self._exit_once(
                "livekit-agent's AgentSession closed after an unrecoverable "
                "pipeline error; exiting so the service manager starts a fresh "
                "voice agent session",
                rate_limit_key=_AGENT_SESSION_UNRECOVERABLE_RATE_LIMIT_KEY,
            )
            return
        if _INIT_FAILURE_MESSAGE not in message:
            return
        now = time.monotonic()
        with self._lock:
            cutoff = now - WORKER_INIT_FAILURE_WINDOW_SECONDS
            self._failure_times = [
                failed_at for failed_at in self._failure_times if failed_at >= cutoff
            ]
            self._failure_times.append(now)
            if len(self._failure_times) < WORKER_INIT_FAILURE_THRESHOLD:
                return
        self._exit_once(
            "livekit-agent job workers failed to initialize "
            f"{WORKER_INIT_FAILURE_THRESHOLD} times within "
            f"{WORKER_INIT_FAILURE_WINDOW_SECONDS:.0f}s; exiting so the "
            "service manager restarts the agent under the current environment"
        )

    def _exit_once(self, reason: str, *, rate_limit_key: str | None = None) -> None:
        with self._lock:
            if self._exiting:
                return
            if rate_limit_key and not _rate_limited_exit_allowed(rate_limit_key):
                self._exiting = True
                logger.critical(
                    "%s; restart rate limit reached (%d exits within %.0fs), "
                    "leaving process up to avoid restart churn",
                    reason,
                    WORKER_EXIT_RATE_LIMIT_THRESHOLD,
                    WORKER_EXIT_RATE_LIMIT_WINDOW_SECONDS,
                )
                return
            self._exiting = True
        logger.critical(reason)
        self._initiate_exit()

    def _initiate_exit(self) -> None:
        threading.Thread(
            target=_force_exit_after_grace,
            name="openbase-worker-watchdog-exit",
            daemon=True,
        ).start()
        os.kill(os.getpid(), signal.SIGTERM)


def _force_exit_after_grace() -> None:
    time.sleep(_FORCED_EXIT_GRACE_SECONDS)
    os._exit(1)


def _rate_limited_exit_allowed(key: str) -> bool:
    """Persistently bound process-exit self-heal loops across restarts."""
    now = time.time()
    cutoff = now - WORKER_EXIT_RATE_LIMIT_WINDOW_SECONDS
    try:
        payload = _read_rate_limit_state(_EXIT_RATE_LIMIT_STATE_PATH)
    except Exception:
        logger.warning(
            "Unable to read LiveKit watchdog restart rate-limit state; "
            "allowing self-heal exit",
            exc_info=True,
        )
        return True

    recent_exits = [
        timestamp
        for timestamp in _timestamps_for_key(payload, key)
        if timestamp >= cutoff
    ]
    if len(recent_exits) >= WORKER_EXIT_RATE_LIMIT_THRESHOLD:
        payload[key] = recent_exits
        _write_rate_limit_state(payload)
        return False

    payload[key] = [*recent_exits, now]
    _write_rate_limit_state(payload)
    return True


def _read_rate_limit_state(state_path: Path) -> dict[str, list[float]]:
    if not state_path.is_file():
        return {}
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _timestamps_for_key(payload: dict[str, list[float]], key: str) -> list[float]:
    values = payload.get(key, [])
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, int | float)]


def _write_rate_limit_state(payload: dict[str, list[float]]) -> None:
    try:
        _EXIT_RATE_LIMIT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _EXIT_RATE_LIMIT_STATE_PATH.write_text(
            json.dumps(payload, sort_keys=True),
            encoding="utf-8",
        )
    except Exception:
        logger.warning(
            "Unable to write LiveKit watchdog restart rate-limit state",
            exc_info=True,
        )


def install_worker_failure_watchdog() -> WorkerFailureWatchdog:
    handler = WorkerFailureWatchdog()
    logging.getLogger("livekit.agents").addHandler(handler)
    return handler
