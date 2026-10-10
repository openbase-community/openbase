"""WebSocket consumers for real-time thread updates."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs

from channels.generic.websocket import (
    AsyncJsonWebsocketConsumer,
    AsyncWebsocketConsumer,
)
from super_agents.app_permissions import DEFAULT_APPROVAL_REQUESTS_FILE
from watchfiles import awatch

from openbase_coder_cli import mcp_gateway
from openbase_coder_cli.openbase_coder_cli_app.approvals import (
    pending_approval_requests,
)
from openbase_coder_cli.openbase_coder_cli_app.ios_app_control import (
    COMMAND_ID_RE,
    ack_group_name,
)
from openbase_coder_cli.openbase_coder_cli_app.notification_runtime import (
    run_notification_sweep as _run_notification_sweep,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_errors import (
    thread_error_code,
    thread_error_message,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_metadata import (
    annotate_thread_payload,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_models import (
    validate_model_for_thread,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_terminal import (
    AGENT_TERMINAL_KEY_PREFIX,
    DEFAULT_COLS,
    DEFAULT_ROWS,
    TerminalSession,
    TerminalUnavailableError,
    display_command,
    get_terminal_registry,
    resolve_agent_terminal_launch,
    resolve_terminal_launch,
    terminal_supported,
)
from openbase_coder_cli.thread_model_overrides import (
    get_thread_model_override,
    set_thread_model_override,
)
from openbase_coder_cli.thread_sync.session_manager import get_session_manager

logger = logging.getLogger(__name__)


def _friendly_error(exc: Exception) -> str:
    """Extract a safe human-readable message from manager errors."""
    return thread_error_message(exc)


async def _apply_turn_model(manager, thread_id: str, content: dict) -> str | None:
    """Validate and persist an optional per-turn model switch.

    Mirrors the HTTP turn endpoints: a `model` in the payload must stay on the
    thread's own backend and is stored as the thread's model override so later
    turns keep using it. Raises ValueError on unknown or cross-backend models.
    """
    model = content.get("model")
    if not model or not isinstance(model, str):
        return None
    thread = await manager.get_thread_state(thread_id)
    if thread is None:
        raise ValueError(f"Thread {thread_id} not found")
    model = validate_model_for_thread(
        thread.backend,
        model,
        current_model=get_thread_model_override(thread_id) or thread.model,
    )
    set_thread_model_override(thread_id, model)
    return model


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
                model = await _apply_turn_model(manager, self.thread_id, content)
                await manager.start_turn(self.thread_id, prompt, model=model)
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
                model = await _apply_turn_model(manager, self.thread_id, content)
                result = await manager.queue_turn(self.thread_id, prompt, model=model)
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


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


class ThreadTerminalConsumer(AsyncWebsocketConsumer):
    """A thread's native backend TUI (Codex / Claude Code) over a PTY.

    Binary frames carry raw terminal bytes both ways. Text frames are JSON
    control messages: client ``resize`` / ``restart``; server ``ready`` /
    ``exit`` / ``error``. The PTY outlives the socket (see thread_terminal),
    so reconnecting resumes the same TUI instead of relaunching it.
    """

    session: TerminalSession | None = None

    async def connect(self):
        if self.scope.get("user") != "authenticated":
            await self.close(code=4001)
            return
        self.thread_id = self.scope["url_route"]["kwargs"]["thread_id"]
        await self._accept_viewer()
        await self._attach(restart=False)

    async def _accept_viewer(self) -> None:
        params = parse_qs(self.scope.get("query_string", b"").decode("utf-8"))
        self._cols = _as_int(params.get("cols", [None])[0], DEFAULT_COLS)
        self._rows = _as_int(params.get("rows", [None])[0], DEFAULT_ROWS)
        # A reconnecting client that still shows the screen asks for no
        # replay (`replay=0`); the forced redraw repaints it instead.
        self._send_replay = params.get("replay", ["1"])[0] != "0"
        self._outbox: asyncio.Queue[tuple[str, object] | None] = asyncio.Queue()
        await self.accept()
        self._sender = asyncio.create_task(self._drain_outbox())

    async def _attach(self, *, restart: bool) -> None:
        registry = get_terminal_registry()
        session = None if restart else registry.get(self.thread_id)
        reattached = session is not None and session.running
        if not reattached:
            try:
                launch = await self._resolve_launch()
                session = registry.open(self.thread_id, launch, self._cols, self._rows)
            except TerminalUnavailableError as exc:
                await self._send_control("error", {"message": str(exc)})
                return
            except OSError as exc:
                logger.exception(
                    "thread_terminal launch failed thread=%s", self.thread_id
                )
                await self._send_control(
                    "error", {"message": f"Unable to start the terminal: {exc}"}
                )
                return
        assert session is not None
        self.session = session
        replay = session.attach(self._on_session_event)
        await self._send_control("ready", self._ready_payload(session, reattached))
        if replay and self._send_replay:
            self._outbox.put_nowait(("bytes", replay))
        # A reattached TUI repaints at this viewer's size.
        session.resize(self._cols, self._rows, force_redraw=reattached)

    def _ready_payload(self, session: TerminalSession, reattached: bool) -> dict:
        launch = session.launch
        return {
            "backend": launch.backend,
            "target": launch.target,
            "command": shlex.join([Path(launch.argv[0]).name, *launch.argv[1:]]),
            "cwd": launch.cwd,
            "reattached": reattached,
        }

    async def _resolve_launch(self):
        if not terminal_supported():
            raise TerminalUnavailableError(
                "The thread terminal is not available on Windows yet."
            )
        manager = get_session_manager()
        try:
            thread = await manager.get_thread_state(self.thread_id)
        except (ValueError, RuntimeError) as exc:
            raise TerminalUnavailableError(_friendly_error(exc)) from exc
        if thread is None:
            raise TerminalUnavailableError("Thread not found.")
        return resolve_terminal_launch(
            thread_id=self.thread_id,
            backend=thread.backend,
            backend_session_id=thread.backend_session_id,
            directory=thread.directory,
        )

    def _on_session_event(self, kind: str, payload: object) -> None:
        if kind == "output":
            self._outbox.put_nowait(("bytes", payload))
        elif kind == "exit":
            self._outbox.put_nowait(("exit", payload))

    async def _drain_outbox(self) -> None:
        while True:
            item = await self._outbox.get()
            if item is None:
                return
            kind, payload = item
            if kind == "bytes":
                await self.send(bytes_data=payload)
            elif kind == "exit":
                await self._send_control("exit", {"code": payload})
            else:
                await self.send(text_data=payload)

    async def _send_control(self, kind: str, data: dict) -> None:
        self._outbox.put_nowait(("text", json.dumps({"type": kind, "data": data})))

    async def receive(self, text_data=None, bytes_data=None):
        if bytes_data is not None:
            if self.session is not None:
                self.session.write(bytes_data)
            return
        if not text_data:
            return
        try:
            message = json.loads(text_data)
        except json.JSONDecodeError:
            return
        kind = message.get("type") if isinstance(message, dict) else None
        if kind == "input" and isinstance(message.get("data"), str):
            if self.session is not None:
                self.session.write(message["data"].encode("utf-8"))
        elif kind == "resize":
            self._cols = _as_int(message.get("cols"), self._cols)
            self._rows = _as_int(message.get("rows"), self._rows)
            if self.session is not None:
                self.session.resize(self._cols, self._rows)
        elif kind == "restart":
            self._detach()
            await self._attach(restart=True)

    def _detach(self) -> None:
        if self.session is not None:
            self.session.detach(self._on_session_event)
            self.session = None

    async def disconnect(self, close_code):
        self._detach()
        if hasattr(self, "_outbox"):
            self._outbox.put_nowait(None)


class AgentTerminalConsumer(ThreadTerminalConsumer):
    """A NEW Codex / Claude Code session in a PTY, for a remote terminal.

    An Openbase Sync edge's ``openbase-coder codex|claude`` runs the session
    on this hub: it connects to ``ws/agent-terminals/``, sends ``start``
    (agent, home-relative cwd, args, cols, rows), and gets ``ready`` with the
    session ``id``. The session is launched here with this computer's
    Openbase profile (``agent_launch``), so it is as visible and steerable as
    one started locally. The PTY outlives the socket: a dropped client
    reattaches with ``ws/agent-terminals/<id>/``. Other frames are those of
    ``ThreadTerminalConsumer``.
    """

    _launch = None
    _notices: tuple[str, ...] = ()

    async def connect(self):
        if self.scope.get("user") != "authenticated":
            await self.close(code=4001)
            return
        session_id = self.scope["url_route"]["kwargs"].get("session_id")
        await self._accept_viewer()
        if not session_id:
            self.thread_id = None  # waits for the client's ``start``
            return
        self.thread_id = session_id
        if not session_id.startswith(AGENT_TERMINAL_KEY_PREFIX):
            await self._send_control("error", {"message": "Unknown session."})
            return
        existing = get_terminal_registry().get(session_id)
        if existing is not None and existing.running:
            self._launch = existing.launch
        await self._attach(restart=False)

    async def _resolve_launch(self):
        if self._launch is None:
            raise TerminalUnavailableError("This session has ended.")
        return self._launch

    async def receive(self, text_data=None, bytes_data=None):
        if self.thread_id is None:
            if text_data:
                await self._start(text_data)
            return
        await super().receive(text_data=text_data, bytes_data=bytes_data)

    async def _start(self, text_data: str) -> None:
        try:
            message = json.loads(text_data)
        except json.JSONDecodeError:
            return
        if not isinstance(message, dict) or message.get("type") != "start":
            return
        if not terminal_supported():
            await self._send_control(
                "error", {"message": "Remote sessions are not available on Windows."}
            )
            return
        self._cols = _as_int(message.get("cols"), self._cols)
        self._rows = _as_int(message.get("rows"), self._rows)
        try:
            launch, notices = await asyncio.to_thread(
                resolve_agent_terminal_launch,
                agent=message.get("agent"),
                cwd=message.get("cwd"),
                args=message.get("args", []),
            )
        except TerminalUnavailableError as exc:
            await self._send_control("error", {"message": str(exc)})
            return
        self._launch = launch
        self._notices = notices
        self.thread_id = f"{AGENT_TERMINAL_KEY_PREFIX}{uuid.uuid4().hex}"
        await self._attach(restart=True)

    def _ready_payload(self, session: TerminalSession, reattached: bool) -> dict:
        launch = session.launch
        return {
            "id": self.thread_id,
            "backend": launch.backend,
            "target": launch.target,
            "command": display_command(launch.argv),
            "cwd": launch.cwd,
            "notices": list(self._notices),
            "reattached": reattached,
        }


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
                watch_filter=lambda _change, changed_path: (
                    Path(changed_path) == store_path
                ),
                debounce=50,
                step=25,
            ):
                if channel_layer is not None:
                    await channel_layer.group_send(
                        self.group_name,
                        {"type": "approval_requests_changed"},
                    )
                # New approvals should reach the notification feed within
                # this same wakeup, not on the next poll tick.
                await _run_notification_sweep(force=True)
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
    return (
        Path(configured).expanduser() if configured else DEFAULT_APPROVAL_REQUESTS_FILE
    )


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


class _NotificationStoreWatcher:
    """Broadcast notification-store changes while socket clients exist.

    Every producer mutation rewrites the store file atomically, so the file
    watcher broadcasts the resulting feed changes. The server lifespan owns
    notification production independently of these connections.
    """

    group_name = "notifications"

    def __init__(self) -> None:
        self._connections = 0
        self._task: asyncio.Task[None] | None = None

    def acquire(self) -> None:
        self._connections += 1
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

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

    async def _run(self) -> None:
        from openbase_coder_cli.openbase_coder_cli_app.notification_store import (
            notifications_store_path,
        )

        await self._watch(notifications_store_path())

    async def _watch(self, store_path: Path) -> None:
        from channels.layers import get_channel_layer

        store_path.parent.mkdir(parents=True, exist_ok=True)
        channel_layer = get_channel_layer()
        try:
            async for _changes in awatch(
                store_path.parent,
                watch_filter=lambda _change, changed_path: (
                    Path(changed_path) == store_path
                ),
                debounce=50,
                step=25,
            ):
                if channel_layer is not None:
                    await channel_layer.group_send(
                        self.group_name,
                        {"type": "notifications_changed"},
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Notification store watcher failed; closing live clients")
            if channel_layer is not None:
                await channel_layer.group_send(
                    self.group_name,
                    {"type": "notifications_unavailable"},
                )


_notification_store_watcher = _NotificationStoreWatcher()


class NotificationsConsumer(AsyncJsonWebsocketConsumer):
    """Push notification snapshots when the store changes."""

    group_name = _NotificationStoreWatcher.group_name

    async def connect(self):
        self._watching = False
        if self.scope.get("user") != "authenticated":
            await self.close(code=4001)
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        _notification_store_watcher.acquire()
        self._watching = True
        await self._send_snapshot()

    async def disconnect(self, close_code):
        if getattr(self, "_watching", False):
            await _notification_store_watcher.release()
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if content.get("action") == "refresh":
            await _run_notification_sweep(force=True)
            await self._send_snapshot()

    async def notifications_changed(self, event):
        await self._send_snapshot()

    async def notifications_unavailable(self, event):
        await self.close(code=1011)

    async def _send_snapshot(self):
        from asgiref.sync import sync_to_async

        from openbase_coder_cli.openbase_coder_cli_app.notification_store import (
            list_notifications,
        )

        try:
            payload = await sync_to_async(list_notifications, thread_sensitive=False)()
        except (ValueError, RuntimeError) as exc:
            logger.warning("Unable to load notifications: %s", exc)
            await self.close(code=1011)
            return
        await self.send_json({"type": "notifications", "data": payload})


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
        logger.info(
            "dispatch_timing stage=ios_control_ack_received command_id=%s server_received_unix_ms=%.3f",
            command_id,
            time.time() * 1000,
        )
        ack = {"type": "ios_app_control_ack", "command_id": command_id}
        if type(content.get("opened")) is bool:
            # open_url acks report whether the URL actually opened.
            ack["opened"] = content["opened"]
            if type(content.get("notified")) is bool:
                ack["notified"] = content["notified"]
            # Loopback-forward outcome (step 4 phones): started|vpn_down|failed|unsupported.
            if isinstance(content.get("forward"), str):
                ack["forward"] = content["forward"][:32]
                if isinstance(content.get("forward_error"), str):
                    ack["forward_error"] = content["forward_error"][:1024]
            if isinstance(content.get("error"), str):
                ack["error"] = content["error"][:1024]
        state = content.get("call_state")
        if (
            isinstance(state, dict)
            and all(
                type(state.get(key)) is bool
                for key in ("connected", "muted", "speaker", "active")
            )
            and type(content.get("applied")) is bool
        ):
            ack["call_state"] = {
                key: state[key] for key in ("connected", "muted", "speaker", "active")
            }
            ack["applied"] = content["applied"]
            if isinstance(content.get("error"), str):
                ack["error"] = content["error"][:1024]
        await self.channel_layer.group_send(ack_group_name(command_id), ack)

    async def ios_app_control(self, event):
        await self.send_json({"type": "ios_app_control", "data": event["data"]})


class McpGatewayConsumer(AsyncWebsocketConsumer):
    """Bridge to a stdio MCP server this machine serves (see ``mcp_gateway``).

    One JSON-RPC message per text frame each way. Only servers listed in the
    gateway config are reachable, only by the owner, and each socket gets its
    own process, ended when the socket closes.
    """

    bridge: mcp_gateway.GatewayBridge | None = None
    _inbox: asyncio.Queue[str | None] | None = None
    _tasks: tuple[asyncio.Task, ...] = ()

    async def connect(self):
        if self.scope.get("user") != "authenticated":
            await self.close(code=mcp_gateway.CLOSE_UNAUTHENTICATED)
            return
        name = self.scope["url_route"]["kwargs"]["name"]
        server = mcp_gateway.served_servers().get(name)
        await self.accept()
        if server is None:
            await self.close(
                code=mcp_gateway.CLOSE_UNKNOWN_SERVER,
                reason=f"{name} is not served on this machine",
            )
            return
        bridge = mcp_gateway.GatewayBridge(server, self._send_text)
        try:
            await bridge.start()
        except OSError as exc:
            logger.warning("mcp-gateway %s failed to start: %s", name, exc)
            await self.close(
                code=mcp_gateway.CLOSE_START_FAILED,
                reason=f"{name} could not be started",
            )
            return
        self.bridge = bridge
        self._inbox = asyncio.Queue()
        self._tasks = (
            asyncio.create_task(self._write_inbox()),
            asyncio.create_task(self._close_when_process_exits()),
        )
        logger.info("mcp-gateway %s: bridge opened", name)

    async def _send_text(self, text: str) -> None:
        await self.send(text_data=text)

    async def _write_inbox(self) -> None:
        # Writes happen off the receive path so a process that stops reading
        # stdin cannot stall this consumer's disconnect handling.
        assert self._inbox is not None and self.bridge is not None
        while True:
            text = await self._inbox.get()
            if text is None:
                return
            if not await self.bridge.to_process(text):
                return

    async def _close_when_process_exits(self) -> None:
        assert self.bridge is not None
        await self.bridge.wait_stdout_closed()
        await self.close(
            code=mcp_gateway.CLOSE_PROCESS_EXITED,
            reason=f"{self.bridge.server.name} exited",
        )

    async def receive(self, text_data=None, bytes_data=None):
        if self._inbox is None:
            return
        if text_data is None and bytes_data is not None:
            text_data = bytes_data.decode("utf-8", errors="replace")
        if text_data:
            self._inbox.put_nowait(text_data)

    async def disconnect(self, close_code):
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current and not task.done():
                task.cancel()
        self._tasks = ()
        self._inbox = None
        bridge, self.bridge = self.bridge, None
        if bridge is not None:
            await bridge.stop()
            logger.info("mcp-gateway %s: bridge closed", bridge.server.name)
