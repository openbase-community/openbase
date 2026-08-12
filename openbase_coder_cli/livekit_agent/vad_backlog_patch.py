"""Bound the Silero VAD input backlog so ``user_state`` describes the present.

On degraded links audio reaches the agent in bursts, and under CPU or
event-loop pressure Silero inference falls behind the audio clock (Aug 2026
drive logs: up to 84s). The VAD stream buffers input in an unbounded channel,
so once behind, ``session.user_state`` describes the past — which delays the
mute lifecycle and can report "quiet" while the user is actually speaking.

This patch swaps the stream's input channel for a bounded one: when buffered
audio exceeds ``MAX_VAD_INPUT_BACKLOG_SECONDS`` the oldest frames are dropped
(flush sentinels are preserved) so inference fast-forwards to near-now. Every
drop is reported to a registered listener so the delivery ledger can restart
quiet-floor verification — skipped audio may have contained speech, so quiet
observed before the gap must not count toward a mute.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable

from livekit import rtc
from livekit.agents.utils.aio import ChanClosed
from livekit.plugins.silero import vad as silero_vad

logger = logging.getLogger(__name__)

MAX_VAD_INPUT_BACKLOG_SECONDS = 3.0
VAD_INPUT_BACKLOG_KEEP_SECONDS = 1.0
_DROP_LOG_INTERVAL_SECONDS = 1.0

_backlog_listener: Callable[[float], None] | None = None
_installed = False


def set_vad_backlog_listener(listener: Callable[[float], None] | None) -> None:
    """Register the per-session callback invoked with dropped seconds."""
    global _backlog_listener
    _backlog_listener = listener


class BoundedVadInputChannel:
    """Drop-in for the VAD stream's input ``Chan`` with a bounded backlog.

    Implements the surface the VAD pipeline actually uses: ``send_nowait``,
    ``close``, ``closed``, and async iteration.
    """

    def __init__(
        self,
        *,
        max_backlog_seconds: float = MAX_VAD_INPUT_BACKLOG_SECONDS,
        keep_seconds: float = VAD_INPUT_BACKLOG_KEEP_SECONDS,
    ) -> None:
        self._max_backlog_seconds = max_backlog_seconds
        self._keep_seconds = keep_seconds
        self._items: deque = deque()
        self._buffered_seconds = 0.0
        self._closed = False
        self._wakeup = asyncio.Event()
        self._dropped_since_log = 0.0
        self._last_drop_log_at = float("-inf")

    @property
    def closed(self) -> bool:
        return self._closed

    def send_nowait(self, item) -> None:
        if self._closed:
            raise ChanClosed
        self._items.append(item)
        self._buffered_seconds += _item_seconds(item)
        if self._buffered_seconds > self._max_backlog_seconds:
            self._drop_stale()
        self._wakeup.set()

    def close(self) -> None:
        self._closed = True
        self._wakeup.set()

    def __aiter__(self) -> "BoundedVadInputChannel":
        return self

    async def __anext__(self):
        while True:
            if self._items:
                item = self._items.popleft()
                self._buffered_seconds = max(
                    0.0, self._buffered_seconds - _item_seconds(item)
                )
                return item
            if self._closed:
                raise StopAsyncIteration
            self._wakeup.clear()
            await self._wakeup.wait()

    def _drop_stale(self) -> None:
        dropped_seconds = 0.0
        preserved: deque = deque()
        while self._items and self._buffered_seconds > self._keep_seconds:
            item = self._items.popleft()
            if isinstance(item, rtc.AudioFrame):
                seconds = _item_seconds(item)
                self._buffered_seconds -= seconds
                dropped_seconds += seconds
            else:
                # Flush sentinels are segment barriers; never drop them.
                preserved.append(item)
        while preserved:
            self._items.appendleft(preserved.pop())
        if dropped_seconds <= 0.0:
            return
        self._dropped_since_log += dropped_seconds
        now = time.monotonic()
        if now - self._last_drop_log_at >= _DROP_LOG_INTERVAL_SECONDS:
            logger.warning(
                "dispatch_timing stage=vad_backlog_dropped dropped_ms=%d "
                "buffered_ms=%d max_backlog_ms=%d",
                int(self._dropped_since_log * 1000),
                int(self._buffered_seconds * 1000),
                int(self._max_backlog_seconds * 1000),
            )
            self._dropped_since_log = 0.0
            self._last_drop_log_at = now
        listener = _backlog_listener
        if listener is not None:
            try:
                listener(dropped_seconds)
            except Exception:
                logger.warning(
                    "dispatch_timing stage=vad_backlog_listener_failed",
                    exc_info=True,
                )


def _item_seconds(item) -> float:
    if isinstance(item, rtc.AudioFrame) and item.sample_rate:
        return item.samples_per_channel / item.sample_rate
    return 0.0


def install_vad_backlog_patch() -> None:
    """Bound the input backlog of every Silero VAD stream in this process.

    The replacement happens synchronously inside ``__init__`` before the
    stream's main task has had a chance to run, so no frame can be consumed
    from (or left behind in) the original channel.
    """
    global _installed
    if _installed:
        return
    _installed = True

    original_init = silero_vad.VADStream.__init__

    def patched_init(self, *args, **kwargs) -> None:
        original_init(self, *args, **kwargs)
        self._input_ch = BoundedVadInputChannel()

    silero_vad.VADStream.__init__ = patched_init
    logger.info(
        "Installed Silero VAD backlog bound: max=%.1fs keep=%.1fs",
        MAX_VAD_INPUT_BACKLOG_SECONDS,
        VAD_INPUT_BACKLOG_KEEP_SECONDS,
    )
