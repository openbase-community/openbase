import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openbase_coder_cli.openbase_coder_cli_app import notification_runtime
from openbase_coder_cli.thread_sync import session_manager


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["turn/completed", "turn/failed"])
async def test_turn_broadcast_does_not_wait_for_discovery(monkeypatch, method):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_sweep(*, force):
        entered.set()
        await release.wait()

    monkeypatch.setattr(notification_runtime, "run_notification_sweep", slow_sweep)
    manager = session_manager.CodexAppServerSessionManager(client=SimpleNamespace())
    state = SimpleNamespace(model_dump=lambda **kwargs: {})
    monkeypatch.setattr(manager, "get_session_state", AsyncMock(return_value=state))
    broadcast = AsyncMock()
    monkeypatch.setattr(session_manager, "_broadcast", broadcast)
    monkeypatch.setattr(session_manager, "_notify_manual_thread_finished", AsyncMock())
    worker = notification_runtime.request_notification_sweep()
    try:
        await entered.wait()
        await asyncio.wait_for(
            manager._handle_client_event(
                method, {"threadId": "agent-1", "turnId": "turn-1"}
            ),
            timeout=1,
        )
        assert not worker.done()
        types = [call.args[1]["type"] for call in broadcast.await_args_list]
        assert "turn_completed" in types
        assert ("error" in types) == (method == "turn/failed")
    finally:
        release.set()
        await worker


@pytest.mark.asyncio
async def test_requests_coalesce_with_one_trailing_pass(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def sweep(*, force):
        nonlocal calls
        assert force
        calls += 1
        entered.set()
        await release.wait()

    monkeypatch.setattr(notification_runtime, "run_notification_sweep", sweep)
    worker = notification_runtime.request_notification_sweep()
    for _ in range(100):
        assert notification_runtime.request_notification_sweep() is worker
    await entered.wait()
    assert calls == 1
    for _ in range(100):
        assert notification_runtime.request_notification_sweep() is worker
    release.set()
    await worker
    assert calls == 2
    assert not notification_runtime._requested_sweeps


@pytest.mark.asyncio
async def test_cancel_before_first_pass_does_not_leave_stale_worker():
    worker = notification_runtime.request_notification_sweep()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert not notification_runtime._requested_sweeps


@pytest.mark.asyncio
async def test_shutdown_cancels_inflight_discovery(monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def sweep(*, force):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(notification_runtime, "run_notification_sweep", sweep)
    owner = asyncio.create_task(notification_runtime.run_notification_sweeps())
    await entered.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert cancelled.is_set()
    assert not notification_runtime._requested_sweeps
