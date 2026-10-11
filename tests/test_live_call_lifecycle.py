"""Exercise installed RoomIO's disconnect policy and our job cleanup together."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit import rtc
from livekit.agents import AgentSession
from livekit.agents.voice.events import CloseEvent, CloseReason
from livekit.agents.voice.room_io import RoomIO

from openbase_coder_cli.livekit_agent.live_call_lifecycle import (
    bind_live_call_lifecycle,
)


@pytest.fixture
async def call(monkeypatch):
    session = AgentSession()
    # Run the installed SDK's real participant/disconnect policy without
    # starting a network/model. Complete its requested close synchronously.
    monkeypatch.setattr(
        session,
        "_close_soon",
        lambda *, reason: session.emit("close", CloseEvent(reason=reason)),
    )
    room = SimpleNamespace(
        name="owned-call", local_participant=SimpleNamespace(identity="agent")
    )
    callbacks = []
    ctx = SimpleNamespace(
        room=room, shutdown=Mock(), add_shutdown_callback=callbacks.append
    )
    delete = AsyncMock()
    bind_live_call_lifecycle(ctx, session, delete_room=delete)
    io = RoomIO(session, room)
    caller = SimpleNamespace(
        identity="caller",
        sid="PA_caller",
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
        attributes={},
        disconnect_reason=rtc.DisconnectReason.CLIENT_INITIATED,
    )
    # Avoid unrelated input wiring; this is exactly the linked participant
    # future that RoomIO's real disconnect/reconnect handlers consume.
    io._participant_available_fut.set_result(caller)
    monkeypatch.setattr(session, "_on_room_io_participant_linked", Mock())
    return SimpleNamespace(
        session=session,
        io=io,
        caller=caller,
        ctx=ctx,
        delete=delete,
        callbacks=callbacks,
    )


async def test_explicit_hangup_deletes_room_and_ends_job_once(call):
    call.io._on_participant_disconnected(call.caller)
    # A second SDK close event cannot create another room deletion.
    call.session.emit("close", CloseEvent(reason=CloseReason.PARTICIPANT_DISCONNECTED))
    await call.callbacks[0]()
    call.delete.assert_awaited_once_with("owned-call")
    call.ctx.shutdown.assert_called_once_with(reason="caller-disconnected")


@pytest.mark.parametrize(
    "reason",
    [None, rtc.DisconnectReason.STATE_MISMATCH, rtc.DisconnectReason.SERVER_SHUTDOWN],
)
async def test_transient_disconnect_and_rejoin_preserve_session(call, reason):
    history = call.session.history
    call.caller.disconnect_reason = reason
    call.io._on_participant_disconnected(call.caller)
    await asyncio.sleep(0)
    call.delete.assert_not_awaited()
    call.ctx.shutdown.assert_not_called()
    call.io._on_participant_connected(call.caller)
    assert call.io.linked_participant is call.caller
    assert call.session.history is history
    await call.callbacks[0]()


async def test_unrelated_participant_departure_does_not_end_call(call):
    other = SimpleNamespace(identity="announcement-publisher")
    call.io._on_participant_disconnected(other)
    await asyncio.sleep(0)
    call.delete.assert_not_awaited()
    call.ctx.shutdown.assert_not_called()
    assert call.io.linked_participant is call.caller
    await call.callbacks[0]()


async def test_room_delete_timeout_still_releases_job(call):
    call.delete.side_effect = TimeoutError
    call.io._on_participant_disconnected(call.caller)
    await call.callbacks[0]()
    call.ctx.shutdown.assert_called_once_with(reason="caller-disconnected")


async def test_job_shutdown_does_not_reenter_call_cleanup(call):
    call.session.emit("close", CloseEvent(reason=CloseReason.JOB_SHUTDOWN))
    await call.callbacks[0]()
    call.session.emit("close", CloseEvent(reason=CloseReason.PARTICIPANT_DISCONNECTED))
    await asyncio.sleep(0)
    call.delete.assert_not_awaited()
    call.ctx.shutdown.assert_not_called()
