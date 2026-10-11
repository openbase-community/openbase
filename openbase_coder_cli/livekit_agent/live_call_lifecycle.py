"""End the room job on explicit caller hangup in either voice engine."""

import asyncio
import logging

from livekit.agents.voice.events import CloseReason

logger = logging.getLogger(__name__)


def bind_live_call_lifecycle(ctx, session, *, delete_room):
    """Let the SDK distinguish hangup from reconnect, then clean up our job.

    RoomIO closes AgentSession for an explicit participant departure, but
    does not end JobContext or delete the room by default. A transient loss
    does not emit this close event and must keep its session and history.
    """
    cleanup = None

    async def end_call():
        try:
            async with asyncio.timeout(5):
                await delete_room(ctx.room.name)
        except TimeoutError:
            # Leaving the job still releases its models and room participant.
            logger.warning("live_call_room_delete_timeout room=%s", ctx.room.name)
        finally:
            ctx.shutdown(reason="caller-disconnected")

    def on_close(event):
        nonlocal cleanup
        if event.reason == CloseReason.PARTICIPANT_DISCONNECTED and cleanup is None:
            logger.info("live_call_caller_disconnected room=%s", ctx.room.name)
            cleanup = asyncio.create_task(end_call(), name="live-call-hangup")

    async def detach():
        session.off("close", on_close)
        if cleanup is not None:
            await cleanup

    session.on("close", on_close)
    ctx.add_shutdown_callback(detach)
