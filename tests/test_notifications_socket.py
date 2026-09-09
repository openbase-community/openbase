from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from asgiref.testing import ApplicationCommunicator  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    consumers,
    notification_store,
)


async def _connected_communicator():
    scope = {
        "type": "websocket",
        "path": "/ws/notifications/",
        "headers": [],
        "user": "authenticated",
        "url_route": {"kwargs": {}},
    }
    communicator = ApplicationCommunicator(
        consumers.NotificationsConsumer.as_asgi(),
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


async def test_notifications_socket_sends_initial_and_changed_snapshots(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    watcher = MagicMock()
    watcher.release = AsyncMock()

    with patch.object(consumers, "_notification_store_watcher", watcher):
        communicator = await _connected_communicator()
        snapshot = await _receive_json(communicator)
        assert snapshot == {
            "type": "notifications",
            "data": {"notifications": [], "unread_count": 0},
        }

        notification_store.upsert_notification(
            "thread", "t-1", title="My thread", body="Done", thread_id="t-1"
        )
        await communicator.send_input({"type": "notifications.changed"})
        snapshot = await _receive_json(communicator)
        assert snapshot["type"] == "notifications"
        assert snapshot["data"]["unread_count"] == 1
        assert snapshot["data"]["notifications"][0]["id"] == "thread:t-1"

        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        await communicator.wait(timeout=5)

    watcher.acquire.assert_called_once_with()
    watcher.release.assert_awaited_once_with()


async def test_notifications_socket_rejects_unauthenticated_clients():
    scope = {
        "type": "websocket",
        "path": "/ws/notifications/",
        "headers": [],
        "user": None,
        "url_route": {"kwargs": {}},
    }
    communicator = ApplicationCommunicator(
        consumers.NotificationsConsumer.as_asgi(),
        scope,
    )

    await communicator.send_input({"type": "websocket.connect"})
    closed = await communicator.receive_output(timeout=5)

    assert closed == {"type": "websocket.close", "code": 4001}
    await communicator.wait(timeout=5)


async def test_notification_store_watcher_broadcasts_file_changes(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    changed = asyncio.Event()

    class ChannelLayer:
        async def group_send(self, group_name, event):
            if event == {"type": "notifications_changed"}:
                assert group_name == "notifications"
                changed.set()

    monkeypatch.setattr("channels.layers.get_channel_layer", lambda: ChannelLayer())
    # Keep the sweep inert so the watcher's tick loop can't hit real state.
    monkeypatch.setattr(
        "openbase_coder_cli.openbase_coder_cli_app.notification_producers."
        "sync_notification_producers",
        lambda force=False: None,
    )
    watcher = consumers._NotificationStoreWatcher()
    watcher.acquire()
    try:
        await asyncio.sleep(0.1)
        notification_store.upsert_notification("thread", "t-1", title="T")
        await asyncio.wait_for(changed.wait(), timeout=5)
    finally:
        await watcher.release()
