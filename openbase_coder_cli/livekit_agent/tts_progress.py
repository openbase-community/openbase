"""Bound silent synthesis hangs without treating recoverable jitter as failure."""

import asyncio
import time

from livekit.agents import APIError


class TTSStreamStalled(APIError):
    def __init__(self) -> None:
        # Replaying a partial answer automatically would duplicate audible words.
        super().__init__("TTS stream stopped making audio progress", retryable=False)


class TTSProgressGuard:
    """A deadline starts at text submission, even if iteration began earlier.

    Only audio advances it: provider metadata cannot keep a silent stream alive.
    The outer safety bound leaves room for the provider's receive timeout and
    recovery. It is separate from local playback, which the delivery ledger owns.
    """

    def __init__(self, *, first_audio_seconds: float = 180, audio_gap_seconds: float = 90):
        self.first_audio_seconds = first_audio_seconds
        self.audio_gap_seconds = audio_gap_seconds
        self._submitted = asyncio.Event()
        self._submitted_at: float | None = None
        self._audio_at: float | None = None
        self._failed = False

    def submitted(self) -> None:
        if self._submitted_at is None:
            self._submitted_at = time.monotonic()
            self._submitted.set()

    def audio_received(self) -> None:
        self._audio_at = time.monotonic()

    async def next_event(self, stream):
        if self._failed:
            raise TTSStreamStalled()
        pending = asyncio.ensure_future(stream.__anext__())
        submitted = None
        try:
            if self._submitted_at is None:
                submitted = asyncio.create_task(self._submitted.wait())
                await asyncio.wait((pending, submitted), return_when=asyncio.FIRST_COMPLETED)
                if pending.done():
                    return pending.result()
            anchor = self._audio_at if self._audio_at is not None else self._submitted_at
            budget = self.audio_gap_seconds if self._audio_at is not None else self.first_audio_seconds
            remaining = max(0, anchor + budget - time.monotonic())
            done, _ = await asyncio.wait((pending,), timeout=remaining)
            if done:
                return pending.result()
            self._failed = True
            raise TTSStreamStalled()
        finally:
            tasks = [pending] + ([submitted] if submitted is not None else [])
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
