from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from asgiref.testing import ApplicationCommunicator  # noqa: E402
from super_agents.app_permissions import write_permission_store  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import consumers  # noqa: E402


async def _connected_communicator():
    scope = {
        "type": "websocket",
        "path": "/ws/approval-requests/",
        "headers": [],
        "user": "authenticated",
        "url_route": {"kwargs": {}},
    }
    communicator = ApplicationCommunicator(
        consumers.ApprovalRequestsConsumer.as_asgi(),
        scope,
    )
    await communicator.send_input({"type": "websocket.connect"})
    accepted = await communicator.receive_output(timeout=5)
    assert accepted["type"] == "websocket.accept"
    return communicator


async def _receive_json(communicator):
    frame = await communicator.receive_output(timeout=5)
    assert frame["type"] == "websocket.send"
    return json.loads(frame["text"])


async def test_approval_socket_sends_initial_and_changed_snapshots():
    snapshots = [[], [{"id": "approval-1", "method": "tool/requestApproval"}]]
    pending = AsyncMock(side_effect=snapshots)
    watcher = MagicMock()
    watcher.release = AsyncMock()

    with (
        patch.object(consumers, "pending_approval_requests", pending),
        patch.object(consumers, "_approval_store_watcher", watcher),
    ):
        communicator = await _connected_communicator()
        assert await _receive_json(communicator) == {
            "type": "approval_requests",
            "data": {"requests": []},
        }

        await communicator.send_input(
            {
                "type": "approval_requests.changed",
            }
        )
        assert await _receive_json(communicator) == {
            "type": "approval_requests",
            "data": {"requests": snapshots[1]},
        }
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        await communicator.wait(timeout=5)

    watcher.acquire.assert_called_once_with()
    watcher.release.assert_awaited_once_with()


async def test_approval_socket_rejects_unauthenticated_clients():
    scope = {
        "type": "websocket",
        "path": "/ws/approval-requests/",
        "headers": [],
        "user": None,
        "url_route": {"kwargs": {}},
    }
    communicator = ApplicationCommunicator(
        consumers.ApprovalRequestsConsumer.as_asgi(),
        scope,
    )

    await communicator.send_input({"type": "websocket.connect"})
    closed = await communicator.receive_output(timeout=5)

    assert closed == {"type": "websocket.close", "code": 4001}
    await communicator.wait(timeout=5)


async def test_approval_socket_closes_when_snapshot_is_unavailable():
    with (
        patch.object(
            consumers,
            "pending_approval_requests",
            AsyncMock(side_effect=RuntimeError("store unavailable")),
        ),
        patch.object(
            consumers,
            "_approval_store_watcher",
            MagicMock(release=AsyncMock()),
        ),
    ):
        communicator = await _connected_communicator()
        closed = await communicator.receive_output(timeout=5)

    assert closed == {"type": "websocket.close", "code": 1011}
    await communicator.send_input({"type": "websocket.disconnect", "code": 1011})
    await communicator.wait(timeout=5)


async def test_approval_store_watcher_broadcasts_native_file_changes(
    tmp_path,
    monkeypatch,
):
    store_path = tmp_path / "approval-requests.json"
    monkeypatch.setenv("SUPER_AGENTS_APPROVAL_REQUESTS_FILE", str(store_path))
    changed = asyncio.Event()

    class ChannelLayer:
        async def group_send(self, group_name, event):
            if event == {"type": "approval_requests_changed"}:
                assert group_name == "approval_requests"
                changed.set()

    monkeypatch.setattr("channels.layers.get_channel_layer", lambda: ChannelLayer())
    watcher = consumers._ApprovalStoreWatcher()
    watcher.acquire()
    try:
        await asyncio.sleep(0.1)
        write_permission_store(store_path, {"requests": {}, "decisions": {}})
        await asyncio.wait_for(changed.wait(), timeout=5)
    finally:
        await watcher.release()
