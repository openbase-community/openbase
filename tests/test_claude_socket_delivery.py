"""Claude turn delivery through the real manager, channel layer and consumer."""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import django
import pytest
from asgiref.testing import ApplicationCommunicator
from django.test import override_settings
from super_agents.agent_store import Store
from super_agents.claude_sdk import ClaudeAgentSdkClient

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")
django.setup()

from openbase_coder_cli.openbase_coder_cli_app.consumers import (  # noqa: E402
    ThreadConsumer,
)
from openbase_coder_cli.thread_sync.session_manager import (  # noqa: E402
    CodexAppServerSessionManager,
)


async def receive(socket):
    frame = await socket.receive_output(timeout=1)
    return json.loads(frame["text"])


async def connect(thread_id):
    socket = ApplicationCommunicator(
        ThreadConsumer.as_asgi(),
        {
            "type": "websocket",
            "path": f"/ws/threads/{thread_id}/",
            "headers": [],
            "user": "authenticated",
            "url_route": {"kwargs": {"thread_id": thread_id}},
        },
    )
    await socket.send_input({"type": "websocket.connect"})
    assert (await socket.receive_output(timeout=1))["type"] == "websocket.accept"
    return socket


async def disconnect(socket):
    await socket.send_input({"type": "websocket.disconnect", "code": 1000})
    await socket.wait(timeout=1)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_claude_completion_reaches_socket_and_reconnect(
    tmp_path, monkeypatch, legacy, outcome
):
    finished = asyncio.Event()
    release = asyncio.Event()
    store = Store(tmp_path / "claude.sqlite3")

    class SDKClient:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            pass

        async def query(self, prompt):
            pass

        async def disconnect(self):
            pass

        async def receive_response(self):
            yield SimpleNamespace(content=[SimpleNamespace(text="Immediate reply")])
            await release.wait()
            await asyncio.sleep(0.05)
            if outcome == "failed":
                raise RuntimeError("test stream failure")
            if outcome == "cancelled":
                store.update_turn(
                    store.get_session(thread_id).active_turn_id, status="cancelled"
                )
            if outcome == "completed":
                yield SimpleNamespace(content=[SimpleNamespace(text="Final reply")])
            yield SimpleNamespace(result="Immediate reply", num_turns=1)

    original_init = ClaudeAgentSdkClient.__init__

    def isolated_init(self, **kwargs):
        # managed_claude_client passes its own store; the test's wins.
        kwargs.pop("store", None)
        original_init(
            self,
            store=store,
            sdk_loader=lambda: SimpleNamespace(
                ClaudeSDKClient=SDKClient, ClaudeAgentOptions=lambda **options: options
            ),
            **kwargs,
        )

    monkeypatch.setattr(ClaudeAgentSdkClient, "__init__", isolated_init)
    if legacy:
        monkeypatch.delattr(ClaudeAgentSdkClient, "handle_notification")
        monkeypatch.setattr(
            ClaudeAgentSdkClient, "_notify_turn", lambda *args, **kwargs: None
        )
    monkeypatch.setattr(
        "super_agents.claude_options.claude_state_path",
        lambda: tmp_path / "claude.json",
    )
    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.session_manager._notify_manual_thread_finished",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "openbase_coder_cli.openbase_coder_cli_app.notification_runtime.request_notification_sweep",
        lambda: None,
    )
    manager = CodexAppServerSessionManager(
        execution_backend="claude_code", model_for_role=lambda _: None
    )
    monkeypatch.setattr(
        "openbase_coder_cli.openbase_coder_cli_app.consumers.get_session_manager",
        lambda: manager,
    )
    thread_id = (
        await manager._client.start_thread({"name": "delivery", "cwd": str(tmp_path)})
    )["threadId"]
    finish_turn = manager._client._finish_session_turn

    def mark_finished(*args, **kwargs):
        finish_turn(*args, **kwargs)
        finished.set()

    monkeypatch.setattr(manager._client, "_finish_session_turn", mark_finished)
    with override_settings(
        CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    ):
        socket = await connect(thread_id)
        assert (await receive(socket))["type"] == "thread_state"
        await manager.send_message(thread_id, "hello")
        # Streaming content arrives while the SDK is still waiting to finish.
        async with asyncio.timeout(1):
            while True:
                event = await receive(socket)
                if "Immediate reply" in json.dumps(event):
                    break
        assert not finished.is_set()
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)
        async with asyncio.timeout(1):
            while (event := await receive(socket))["type"] != "turn_completed":
                pass
        assert "Immediate reply" in json.dumps(event)
        assert event["data"]["current_turn"] is None
        if not legacy:
            assert not manager._claude_watchers
        await disconnect(socket)
        # A missed completion is recovered from the fresh connect snapshot.
        socket = await connect(thread_id)
        snapshot = await receive(socket)
        assert snapshot["type"] == "thread_state"
        assert "Immediate reply" in json.dumps(snapshot)
        assert snapshot["data"]["current_turn"] is None
        if outcome == "completed":
            assert (
                snapshot["data"]["turn_history"][-1]["accumulated_output"]
                == "Immediate reply\n\nFinal reply"
            )
        await disconnect(socket)
    await manager._client.close()


@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("partial", ["", "own partial"])
async def test_websocket_interrupt_isolates_output_and_stops_owner(
    tmp_path, monkeypatch, foreign, partial
):
    monkeypatch.setenv("SUPER_AGENTS_CLAUDE_CODE_HOME", str(tmp_path))
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "openbase_cloud")
    monkeypatch.setenv("OPENBASE_CLOUD_ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.setattr(
        "super_agents.claude_options.claude_state_path",
        lambda: tmp_path / "claude.json",
    )
    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.session_manager._notify_manual_thread_finished",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "openbase_coder_cli.openbase_coder_cli_app.notification_runtime.request_notification_sweep",
        lambda: None,
    )
    store = Store(tmp_path / "claude.sqlite3")
    waiting, stopped, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    interruptions = []

    class SDKClient:
        def __init__(self, **kwargs):
            self.prompt = ""

        async def connect(self):
            pass

        async def query(self, prompt):
            self.prompt = prompt.rsplit("\n\n", 1)[-1]

        async def interrupt(self):
            interruptions.append(self.prompt)

        async def disconnect(self):
            stopped.set()

        async def receive_response(self):
            if self.prompt == "cancel this":
                if partial:
                    yield SimpleNamespace(content=[SimpleNamespace(text=partial)])
                waiting.set()
                await release.wait()
                yield SimpleNamespace(
                    content=[SimpleNamespace(text="late full answer")]
                )
            else:
                yield SimpleNamespace(content=[SimpleNamespace(text=self.prompt)])
            yield SimpleNamespace(result=self.prompt, num_turns=1)

    original_init = ClaudeAgentSdkClient.__init__

    def isolated_init(self, **kwargs):
        # managed_claude_client passes its own store; the test's wins.
        kwargs.pop("store", None)
        original_init(
            self,
            store=store,
            sdk_loader=lambda: SimpleNamespace(
                ClaudeSDKClient=SDKClient, ClaudeAgentOptions=lambda **options: options
            ),
            **kwargs,
        )

    monkeypatch.setattr(ClaudeAgentSdkClient, "__init__", isolated_init)
    manager = CodexAppServerSessionManager(
        execution_backend="claude_code", model_for_role=lambda _: None
    )
    monkeypatch.setattr(
        "openbase_coder_cli.openbase_coder_cli_app.consumers.get_session_manager",
        lambda: manager,
    )
    owner = manager._client
    canceller = (
        ClaudeAgentSdkClient(backend_identity="openbase_cloud") if foreign else owner
    )
    if foreign:
        monkeypatch.setattr(owner, "cancel_by_label", canceller.cancel_by_label)
    thread_id = (await owner.start_thread({"name": "interrupt", "cwd": str(tmp_path)}))[
        "threadId"
    ]

    async def send(socket, action, **fields):
        await socket.send_input(
            {
                "type": "websocket.receive",
                "text": json.dumps({"action": action, **fields}),
            }
        )

    async def terminal(socket, turn_id):
        async with asyncio.timeout(1):
            while True:
                event = await receive(socket)
                if event["type"] == "turn_completed":
                    turns = event["data"]["turn_history"]
                    if turns and turns[-1]["turn_id"] == turn_id:
                        return event["data"]

    with override_settings(
        CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    ):
        socket = await connect(thread_id)
        try:
            assert (await receive(socket))["type"] == "thread_state"
            first = await manager.send_message(thread_id, "previous answer")
            await terminal(socket, first)
            await send(socket, "start_turn", prompt="cancel this")
            async with asyncio.timeout(1):
                while (event := await receive(socket))["type"] != "turn_started":
                    pass
            turn_id = event["data"]["turn_id"]
            await asyncio.wait_for(waiting.wait(), 1)
            await asyncio.sleep(1)
            await send(socket, "interrupt_turn")
            state = await terminal(socket, turn_id)
            assert state["current_turn"] is None
            assert state["turn_history"][-1]["accumulated_output"] == partial
            await asyncio.wait_for(stopped.wait(), 1)
            assert interruptions == ["cancel this"]
            frozen = store.get_turn(turn_id)
            release.set()
            recovery = await manager.send_message(thread_id, "recovery answer")
            state = await terminal(socket, recovery)
            assert state["turn_history"][-1]["accumulated_output"] == "recovery answer"
            assert store.get_turn(turn_id) == frozen
        finally:
            release.set()
            await disconnect(socket)
            await asyncio.gather(*owner._turn_tasks, return_exceptions=True)
            await owner.close()
            if foreign:
                await canceller.close()
