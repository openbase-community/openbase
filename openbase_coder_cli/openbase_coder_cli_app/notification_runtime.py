"""Notification production tied to the API server, not connected clients."""

import asyncio
import logging
from contextlib import suppress

from asgiref.sync import sync_to_async

from .notification_producers import sync_notification_producers

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 30
_requested_sweeps: dict[
    asyncio.AbstractEventLoop, tuple[asyncio.Task[None], asyncio.Event]
] = {}


async def run_notification_sweep(*, force: bool = False) -> None:
    """Keep discovery off the event loop and isolate failures from turn handling."""
    try:
        await sync_to_async(sync_notification_producers, thread_sensitive=False)(
            force=force
        )
    except Exception:
        # The periodic loop retries; an unavailable source must not stop turns.
        logger.exception("Notification producer sweep failed; will retry")


def request_notification_sweep() -> asyncio.Task[None]:
    """Queue discovery without holding up turn events; coalesce concurrent requests."""
    loop = asyncio.get_running_loop()
    if (active := _requested_sweeps.get(loop)) and not active[0].done():
        active[1].set()
        return active[0]

    pending = asyncio.Event()
    pending.set()

    async def drain() -> None:
        while pending.is_set():
            # A request arriving during discovery needs a trailing pass:
            # that turn may have written a file after this scan started.
            pending.clear()
            await run_notification_sweep(force=True)

    def finished(task: asyncio.Task[None]) -> None:
        if (active := _requested_sweeps.get(loop)) and active[0] is task:
            _requested_sweeps.pop(loop)

    task = loop.create_task(drain(), name="notification-sweep")
    _requested_sweeps[loop] = (task, pending)
    task.add_done_callback(finished)
    return task


async def run_notification_sweeps() -> None:
    """Own background discovery for the server's lifetime, independent of UI clients."""
    try:
        while True:
            await asyncio.shield(request_notification_sweep())
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
    finally:
        if active := _requested_sweeps.get(asyncio.get_running_loop()):
            active[0].cancel()
            with suppress(asyncio.CancelledError):
                await active[0]
