"""Notification production tied to the API server, not connected clients."""

import asyncio
import logging

from asgiref.sync import sync_to_async

from .notification_producers import sync_notification_producers

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 30


async def run_notification_sweep(*, force: bool = False) -> None:
    """Keep discovery off the event loop and isolate failures from turn handling."""
    try:
        await sync_to_async(sync_notification_producers, thread_sensitive=False)(
            force=force
        )
    except Exception:
        # The periodic loop retries; an unavailable source must not stop turns.
        logger.exception("Notification producer sweep failed; will retry")


async def run_notification_sweeps() -> None:
    """Sweep at startup and periodically even when no UI client is connected."""
    while True:
        await run_notification_sweep()
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
