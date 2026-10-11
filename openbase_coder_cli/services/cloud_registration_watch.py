"""Decide when sync-workers re-registers this device with Openbase Cloud.

Registration used to run once an hour, so a logout (which deregisters the
device) followed by a login through any path that skips the login-time
report left the computer invisible to the new account for up to an hour.
The watcher is ticked often and cheaply: it re-registers right away when the
signed-in account changes, retries failures with a capped backoff, and
otherwise refreshes the registration at the regular interval.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 3600.0
RETRY_INITIAL_SECONDS = 30.0
RETRY_MAX_SECONDS = 900.0


@dataclass
class CloudRegistrationWatch:
    owner_sub: Callable[[], str]
    register: Callable[[], object]
    clock: Callable[[], float] = time.monotonic
    refresh_interval: float = REFRESH_INTERVAL_SECONDS
    _registered_sub: str = ""
    _last_success_at: float | None = None
    _retry_at: float | None = None
    _retry_delay: float = RETRY_INITIAL_SECONDS

    def tick(self) -> str:
        """Run one check; returns the reason it acted, or ``"idle"``."""
        sub = self.owner_sub()
        now = self.clock()
        if not sub:
            if self._registered_sub:
                logger.info("cloud_registration logged_out")
            self._reset()
            return "idle"
        if sub != self._registered_sub:
            reason = "account_changed" if self._registered_sub else "first_report"
            self._retry_at = None
            self._retry_delay = RETRY_INITIAL_SECONDS
        elif self._retry_at is not None:
            if now < self._retry_at:
                return "idle"
            reason = "retry"
        elif (
            self._last_success_at is None
            or now - self._last_success_at >= self.refresh_interval
        ):
            reason = "refresh"
        else:
            return "idle"
        self._report(sub, reason, now)
        return reason

    def _reset(self) -> None:
        self._registered_sub = ""
        self._last_success_at = None
        self._retry_at = None
        self._retry_delay = RETRY_INITIAL_SECONDS

    def _report(self, sub: str, reason: str, now: float) -> None:
        self._registered_sub = sub
        result = self.register()
        if getattr(result, "ok", False):
            self._last_success_at = now
            self._retry_at = None
            self._retry_delay = RETRY_INITIAL_SECONDS
            logger.info("cloud_registration report_complete reason=%s", reason)
            return
        if not getattr(result, "supported", True):
            # The cloud predates the endpoint; check again at the next refresh.
            self._last_success_at = now
            self._retry_at = None
            logger.debug("cloud_registration endpoint_unsupported")
            return
        self._retry_at = now + self._retry_delay
        logger.warning(
            "cloud_registration report_failed reason=%s error=%s status=%s retry_in=%ss",
            reason,
            getattr(result, "error", None),
            getattr(result, "status_code", None),
            int(self._retry_delay),
        )
        self._retry_delay = min(self._retry_delay * 2, RETRY_MAX_SECONDS)
