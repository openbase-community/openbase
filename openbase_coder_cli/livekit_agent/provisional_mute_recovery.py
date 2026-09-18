"""Bound provisional microphone holds when STT never produces a transcript."""
import asyncio
from collections.abc import Callable


class ProvisionalMuteRecovery:
    def __init__(self, timeout_seconds: float = 15) -> None:
        self.timeout_seconds = timeout_seconds
        self.task: asyncio.Task[None] | None = None

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None

    def start(self, recover: Callable[[], bool]) -> None:
        self.cancel()
        loop = asyncio.get_running_loop()

        async def wait() -> None:
            await asyncio.sleep(self.timeout_seconds)
            # A real backend turn or announcement can temporarily own the hold.
            while not recover():
                await asyncio.sleep(0.25)

        self.task = loop.create_task(wait(), name="openbase-provisional-mute-recovery")
