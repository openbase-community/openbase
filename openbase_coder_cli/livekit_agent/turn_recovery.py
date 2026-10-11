"""Bounded recovery of a voice turn that failed to reach or start on the backend.

A relay or backend-start hiccup (the Codex app-server restarting, a claude
CLI that takes over a minute to initialize under load, the Cloud model proxy
rolling out, a thread that must be re-resolved) must not cost the caller
their request, and must not be announced as a failure while it can still be
recovered (Gabe, 2026-10-11: "nothing else matters as much as making the
GPT Live agent robust to this"). Both engines run every backend turn through
:func:`run_with_recovery`: the same request is retried with backoff inside a
bounded window, and only when that window is exhausted does the caller hear
a line, which then says what failed and what to do.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from openbase_coder_cli.cloud_model_errors import model_proxy_denial_spoken_message

logger = logging.getLogger(__name__)

DISPATCH_TIMING_LOG = "dispatch_timing"

#: The backend process is not running or not reachable (Codex app-server
#: down, socket refused, "Start the Openbase-managed service").
KIND_BACKEND_DOWN = "backend_down"
#: The turn is alive but the backend cannot answer our polls (poll give-up,
#: timeouts while events still stream).
KIND_BACKEND_SLOW = "backend_slow"
#: The spawned coding agent never finished starting (Claude Agent SDK
#: ``Control request timeout: initialize``, 60 s default).
KIND_START_TIMEOUT = "start_timeout"
#: The model service behind the backend did not answer (Cloud model proxy,
#: gateway, 429/5xx, connection reset).
KIND_MODEL_SERVICE = "model_service"
#: Nothing a retry can fix: the backend refused the request (plan, spend
#: limit, policy). Auth failures never reach here; the client speaks those.
KIND_TERMINAL = "terminal"
#: A failure we cannot name; retried like the others.
KIND_UNKNOWN = "unknown"

_START_TIMEOUT_MARKERS = ("control request timeout: initialize",)
_BACKEND_DOWN_MARKERS = (
    "not running or not reachable",
    "app-server is not running",
    "connection refused",
    "no such file or directory",
    "econnrefused",
    "broken pipe",
    "connection closed",
    "websocket",
)
_BACKEND_SLOW_MARKERS = (
    "turn_wait_poll_gave_up",
    "timed out",
    "timeout",
)
_MODEL_SERVICE_MARKERS = (
    "model proxy",
    "gateway",
    "bad gateway",
    "service unavailable",
    "502",
    "503",
    "504",
    "429",
    "rate limit",
    "overloaded",
    "connection reset",
    "temporarily unavailable",
    "upstream",
)
_TERMINAL_MARKERS = (
    "not available on your plan",
    "spend limit",
    "quota",
    "forbidden",
    "permission denied",
    "invalid request",
    "unsupported model",
)


@dataclass(frozen=True)
class TurnFailure:
    """One failed attempt to run a voice turn, classified for retry."""

    kind: str
    detail: str
    turn_id: str | None = None

    @property
    def retryable(self) -> bool:
        return self.kind != KIND_TERMINAL

    def spoken_line(
        self, agent_label: str, *, elapsed_seconds: float, attempts: int, kept: bool
    ) -> str:
        """What the caller hears once recovery has truly failed."""
        who = agent_label or "the coding agent"
        seconds = max(1, int(round(elapsed_seconds)))
        if self.kind == KIND_BACKEND_DOWN:
            line = (
                f"I couldn't reach the coding backend on your computer for "
                f"{seconds} seconds; it isn't running or isn't answering. Start "
                "or restart the Openbase services, then ask me again."
            )
        elif self.kind == KIND_START_TIMEOUT:
            line = (
                f"{who} couldn't start on your computer: the coding agent took "
                f"more than a minute to launch, {attempts} times in a row. Your "
                "Mac may be overloaded, or a permission dialog may be waiting; "
                "check it, then ask me again."
            )
        elif self.kind == KIND_MODEL_SERVICE:
            line = (
                f"The Openbase Cloud model service didn't answer for {seconds} "
                f"seconds, so {who} couldn't work on that. Try again in a minute."
            )
        elif self.kind == KIND_BACKEND_SLOW:
            line = (
                f"{who} is running but hasn't answered in {seconds} seconds. "
                "I'll pass on the result when it finishes; ask me for it later."
            )
        elif self.kind == KIND_TERMINAL:
            denial = model_proxy_denial_spoken_message(self.detail)
            line = denial or (
                f"{who}'s backend refused that request: {short_detail(self.detail)}"
            )
        else:
            # The raw error is for the logs, not for the caller's ear.
            line = (
                f"That request failed on {who}'s backend {attempts} times. "
                "Ask me again in a moment."
            )
        if kept:
            line += f" I kept your request in {who}'s thread, so it runs once the backend is back."
        return line


class TurnRecoveryFailed(Exception):
    """Recovery gave up: the window closed, the caller moved on, or the
    failure is terminal."""

    def __init__(
        self,
        failure: TurnFailure,
        *,
        attempts: int,
        elapsed_seconds: float,
        reason: str,
    ) -> None:
        super().__init__(f"{failure.kind}: {failure.detail}")
        self.failure = failure
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds
        self.reason = reason


def short_detail(detail: str, limit: int = 120) -> str:
    text = " ".join((detail or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def classify_error_text(detail: str, *, turn_id: str | None = None) -> TurnFailure:
    """Name the failure from its error text (an exception message or the
    backend's ``lastError``)."""
    lowered = (detail or "").lower()
    kind = KIND_UNKNOWN
    if any(marker in lowered for marker in _START_TIMEOUT_MARKERS):
        kind = KIND_START_TIMEOUT
    elif any(marker in lowered for marker in _TERMINAL_MARKERS) or (
        model_proxy_denial_spoken_message(detail) is not None
    ):
        kind = KIND_TERMINAL
    elif any(marker in lowered for marker in _BACKEND_DOWN_MARKERS):
        kind = KIND_BACKEND_DOWN
    elif any(marker in lowered for marker in _MODEL_SERVICE_MARKERS):
        kind = KIND_MODEL_SERVICE
    elif any(marker in lowered for marker in _BACKEND_SLOW_MARKERS):
        kind = KIND_BACKEND_SLOW
    return TurnFailure(kind=kind, detail=detail or "", turn_id=turn_id)


def classify_exception(
    exc: BaseException, *, turn_id: str | None = None
) -> TurnFailure:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return TurnFailure(
            kind=KIND_BACKEND_SLOW,
            detail=str(exc) or type(exc).__name__,
            turn_id=turn_id,
        )
    return classify_error_text(f"{type(exc).__name__}: {exc}", turn_id=turn_id)


def failure_from_result(result: dict[str, Any]) -> TurnFailure | None:
    """The failure a completed ``run_turn`` result reports, if any.

    A failed turn that the client already explained as a backend login
    problem is not a recovery case: that line is spoken as is.
    """
    if not isinstance(result, dict) or not result.get("_livekit_turn_failed"):
        return None
    if result.get("_livekit_backend_auth_failure"):
        return None
    detail = str(result.get("_livekit_turn_error") or "")
    turn_id = str(result.get("_livekit_turn_id") or "") or None
    return classify_error_text(detail, turn_id=turn_id)


RunAttempt = Callable[[int], Awaitable[dict[str, Any]]]
RetryHook = Callable[[TurnFailure, int, float], None]


async def run_with_recovery(
    run: RunAttempt,
    *,
    should_continue: Callable[[], bool],
    window_seconds: float,
    backoff_seconds: Iterable[float],
    on_retry: RetryHook | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    label: str = "",
) -> dict[str, Any]:
    """Run ``run(attempt)`` until it yields a result that is not a failure.

    Retries a retryable failure after the next backoff while the window is
    open and ``should_continue()`` holds (the caller has not moved on, the
    call is up, the route is unchanged). Raises :class:`TurnRecoveryFailed`
    otherwise, carrying the last failure for a specific spoken line.
    """
    backoffs = [float(seconds) for seconds in backoff_seconds] or [3.0]
    started = clock()
    attempt = 0
    while True:
        try:
            result = await run(attempt)
        except asyncio.CancelledError:
            raise
        except TurnRecoveryFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - every backend error is classified
            failure = classify_exception(exc)
        else:
            failure = failure_from_result(result)
            if failure is None:
                if attempt:
                    logger.info(
                        "%s stage=live_turn_recovered label=%s attempts=%d elapsed_ms=%d",
                        DISPATCH_TIMING_LOG,
                        label,
                        attempt + 1,
                        int((clock() - started) * 1000),
                    )
                return result
        attempt += 1
        elapsed = clock() - started
        delay = backoffs[min(attempt - 1, len(backoffs) - 1)]
        reason = None
        if not failure.retryable:
            reason = "terminal"
        elif not should_continue():
            reason = "caller_moved_on"
        elif elapsed + delay > window_seconds:
            reason = "window_exhausted"
        logger.warning(
            "%s stage=live_turn_attempt_failed label=%s attempt=%d kind=%s "
            "turn_id=%s elapsed_ms=%d next=%s detail=%s",
            DISPATCH_TIMING_LOG,
            label,
            attempt,
            failure.kind,
            failure.turn_id or "",
            int(elapsed * 1000),
            reason or f"retry_in_{delay:g}s",
            short_detail(failure.detail),
        )
        if reason is not None:
            raise TurnRecoveryFailed(
                failure, attempts=attempt, elapsed_seconds=elapsed, reason=reason
            )
        if on_retry is not None:
            on_retry(failure, attempt, delay)
        await sleep(delay)
