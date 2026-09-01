"""WebSocket consumers for real-time thread updates."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from super_agents.app_permissions import DEFAULT_APPROVAL_REQUESTS_FILE
from watchfiles import awatch

from openbase_coder_cli.openbase_coder_cli_app.approvals import (
    pending_approval_requests,
)
from openbase_coder_cli.openbase_coder_cli_app.ios_app_control import (
    COMMAND_ID_RE,
    ack_group_name,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_errors import (
    thread_error_code,
    thread_error_message,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_metadata import (
    annotate_thread_payload,
)
from openbase_coder_cli.thread_sync.session_manager import get_session_manager

logger = logging.getLogger(__name__)


def _friendly_error(exc: Exception) -> str:
    """Extract a safe human-readable message from manager errors."""
    return thread_error_message(exc)


class ThreadConsumer(AsyncJsonWebsocketConsumer):
    """WebSocket consumer for a single thread's real-time updates."""

    async def connect(self):
        if self.scope.get("user") != "authenticated":
            await self.close(code=4001)
            return

        self.thread_id = self.scope["url_route"]["kwargs"]["thread_id"]
        self.group_name = f"thread_{self.thread_id}"

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        manager = get_session_manager()
        try:
            thread = await manager.get_thread_state(self.thread_id)
        except (ValueError, RuntimeError) as exc:
            logger.error(
                "Unable to load initial state for thread %s: %s", self.thread_id, exc
            )
            await self.send_json(
                {
                    "type": "error",
                    "data": {
                        "message": _friendly_error(exc),
                        "code": thread_error_code(
                            exc,
                            fallback="thread_state_unavailable",
                        ),
                    },
                }
            )
            return
        if thread:
            await self.send_json(
                {
                    "type": "thread_state",
                    "data": annotate_thread_payload(
                        thread.model_dump(mode="json"),
                        thread_id=self.thread_id,
                    ),
                }
            )

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = content.get("action")
        manager = get_session_manager()

        if action == "start_turn":
            prompt = content.get("prompt", "")
            if not prompt:
                await self.send_json(
                    {"type": "error", "data": {"message": "prompt is required"}}
                )
                return
            try:
                await manager.start_turn(self.thread_id, prompt)
            except (ValueError, RuntimeError) as exc:
                logger.warning(
                    "start_turn failed for thread %s: %s", self.thread_id, exc
                )
                await self.send_json(
                    {"type": "error", "data": {"message": _friendly_error(exc)}}
                )

        elif action == "queue_turn":
            prompt = content.get("prompt", "")
            if not prompt:
                await self.send_json(
                    {"type": "error", "data": {"message": "prompt is required"}}
                )
                return
            try:
                # queue_turn broadcasts refreshed thread_state to the group.
                result = await manager.queue_turn(self.thread_id, prompt)
                await self.send_json({"type": "turn_queued", "data": result})
            except (ValueError, RuntimeError) as exc:
                logger.warning(
                    "queue_turn failed for thread %s: %s", self.thread_id, exc
                )
                await self.send_json(
                    {"type": "error", "data": {"message": _friendly_error(exc)}}
                )

        elif action == "steer_turn":
            prompt = content.get("prompt", "")
            if not prompt:
                await self.send_json(
                    {"type": "error", "data": {"message": "prompt is required"}}
                )
                return
            try:
                # steer_turn broadcasts refreshed thread_state to the group.
                result = await manager.steer_turn(self.thread_id, prompt)
                await self.send_json({"type": "turn_steered", "data": result})
            except (ValueError, RuntimeError) as exc:
                logger.warning(
                    "steer_turn failed for thread %s: %s", self.thread_id, exc
                )
                await self.send_json(
                    {"type": "error", "data": {"message": _friendly_error(exc)}}
                )

        elif action == "interrupt_turn":
            try:
                success = await manager.interrupt_turn(self.thread_id)
            except (ValueError, RuntimeError) as exc:
                logger.warning(
                    "interrupt_turn failed for thread %s: %s", self.thread_id, exc
                )
                await self.send_json(
                    {"type": "error", "data": {"message": _friendly_error(exc)}}
                )
                return
            if not success:
                await self.send_json(
                    {
                        "type": "error",
                        "data": {"message": "No active turn to interrupt"},
                    }
                )

    async def turn_started(self, event):
        await self.send_json({"type": "turn_started", "data": event["data"]})

    async def output_update(self, event):
        await self.send_json({"type": "output_update", "data": event["data"]})

    async def turn_completed(self, event):
        await self.send_json(
            {
                "type": "turn_completed",
                "data": annotate_thread_payload(
                    event["data"],
                    thread_id=self.thread_id,
                ),
            }
        )

    async def thread_state(self, event):
        await self.send_json(
            {
                "type": "thread_state",
                "data": annotate_thread_payload(
                    event["data"],
                    thread_id=self.thread_id,
                ),
            }
        )

    async def error(self, event):
        await self.send_json({"type": "error", "data": event["data"]})


class AllThreadsConsumer(AsyncJsonWebsocketConsumer):
    """Global WebSocket consumer that broadcasts turn lifecycle updates for all threads."""

    async def connect(self):
        if self.scope.get("user") != "authenticated":
            await self.close(code=4001)
            return

        await self.channel_layer.group_add("all_threads", self.channel_name)
        await self.accept()

        manager = get_session_manager()
        try:
            threads = await manager.list_threads()
        except (ValueError, RuntimeError) as exc:
            logger.error("Unable to list threads for all-threads socket: %s", exc)
            await self.send_json(
                {
                    "type": "error",
                    "data": {
                        "message": _friendly_error(exc),
                        "code": "thread_list_unavailable",
                    },
                }
            )
            return
        running = [thread for thread in threads if thread.status == "running"]
        for thread in running:
            await self.send_json(
                {
                    "type": "turn_started",
                    "thread_id": thread.session_id,
                    "data": (
                        thread.current_run.model_dump(mode="json")
                        if thread.current_run
                        else {}
                    ),
                }
            )

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("all_threads", self.channel_name)

    async def receive_json(self, content, **kwargs):
        return

    async def turn_started(self, event):
        await self.send_json(
            {
                "type": "turn_started",
                "thread_id": event["thread_id"],
                "data": event["data"],
            }
        )

    async def turn_completed(self, event):
        await self.send_json(
            {
                "type": "turn_completed",
                "thread_id": event["thread_id"],
                "data": event["data"],
            }
        )

    async def error(self, event):
        await self.send_json(
            {
                "type": "error",
                "thread_id": event["thread_id"],
                "data": event["data"],
            }
        )


class _ApprovalStoreWatcher:
    """Broadcast native approval-store changes while socket clients exist."""

    group_name = "approval_requests"

    def __init__(self) -> None:
        self._connections = 0
        self._task: asyncio.Task[None] | None = None

    def acquire(self) -> None:
        self._connections += 1
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._watch(_approval_store_path()))

    async def release(self) -> None:
        self._connections = max(0, self._connections - 1)
        if self._connections or self._task is None:
            return
        task = self._task
        self._task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _watch(self, store_path: Path) -> None:
        from channels.layers import get_channel_layer

        store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        channel_layer = get_channel_layer()
        try:
            async for _changes in awatch(
                store_path.parent,
                watch_filter=lambda _change, changed_path: Path(changed_path)
                == store_path,
                debounce=50,
                step=25,
            ):
                if channel_layer is not None:
                    await channel_layer.group_send(
                        self.group_name,
                        {"type": "approval_requests_changed"},
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Approval store watcher failed; closing live clients")
            if channel_layer is not None:
                await channel_layer.group_send(
                    self.group_name,
                    {"type": "approval_requests_unavailable"},
                )


def _approval_store_path() -> Path:
    configured = os.environ.get("SUPER_AGENTS_APPROVAL_REQUESTS_FILE")
    return Path(configured).expanduser() if configured else DEFAULT_APPROVAL_REQUESTS_FILE


_approval_store_watcher = _ApprovalStoreWatcher()


class ApprovalRequestsConsumer(AsyncJsonWebsocketConsumer):
    """Push pending approval snapshots when the native queue changes."""

    group_name = _ApprovalStoreWatcher.group_name

    async def connect(self):
        self._watching = False
        if self.scope.get("user") != "authenticated":
            await self.close(code=4001)
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        _approval_store_watcher.acquire()
        self._watching = True
        await self._send_snapshot()

    async def disconnect(self, close_code):
        if getattr(self, "_watching", False):
            await _approval_store_watcher.release()
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if content.get("action") == "refresh":
            await self._send_snapshot()

    async def approval_requests_changed(self, event):
        await self._send_snapshot()

    async def approval_requests_unavailable(self, event):
        await self.close(code=1011)

    async def _send_snapshot(self):
        try:
            requests = await pending_approval_requests()
        except (ValueError, RuntimeError) as exc:
            logger.warning("Unable to load approval requests: %s", exc)
            await self.close(code=1011)
            return
        await self.send_json(
            {"type": "approval_requests", "data": {"requests": requests}}
        )


class IOSAppControlConsumer(AsyncJsonWebsocketConsumer):
    """Foreground iOS app command channel."""

    group_name = "ios_app_control"

    async def connect(self):
        if self.scope.get("user") != "authenticated":
            await self.close(code=4001)
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if content.get("type") != "ios_app_control_ack":
            return
        command_id = content.get("command_id")
        if not isinstance(command_id, str) or not COMMAND_ID_RE.match(command_id):
            return
        await self.channel_layer.group_send(
            ack_group_name(command_id),
            {"type": "ios_app_control_ack", "command_id": command_id},
        )

    async def ios_app_control(self, event):
        await self.send_json({"type": "ios_app_control", "data": event["data"]})
