"""Regression tests for the 2026-09-18 dispatcher voice-session failure.

The shared codex app-server churned on a long turn: it streamed notifications
at ~30/sec while every poll/steer RPC timed out. The dispatcher counted the
poll timeouts to its give-up limit, declared the backend unresponsive, and
dropped every subsequent steered utterance. These pin the fixes: a streaming
backend resets the poll give-up counter, and a timed-out steer is captured in
the local follow-up queue instead of being lost.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openbase_coder_cli.livekit_agent import (
    super_agents_client as super_agents_client_module,
)
from openbase_coder_cli.livekit_agent.super_agents_client import (
    SuperAgentsLiveKitClient,
)
from tests.test_livekit_agent_super_agents_client import (
    FakeFlakyProgressSuperAgentsBackend,
    FakeSuperAgentsBackend,
)


class FakeBusyStreamingBackend(FakeFlakyProgressSuperAgentsBackend):
    """Fails thread/read polls while the turn's notification stream keeps
    advancing — the 2026-09-18 merge-storm signature."""

    def __init__(
        self, failures_before_success: int, streaming_calls: int | None = None
    ) -> None:
        super().__init__(failures_before_success)
        self._turns: dict[str, Any] = {}
        # Stop advancing the notification stream after this many progress
        # calls (None = keep streaming forever).
        self.streaming_calls = streaming_calls

    async def progress_by_label(self, input_data) -> dict[str, Any]:
        if input_data.turn_id and (
            self.streaming_calls is None or self.progress_calls < self.streaming_calls
        ):
            key = f"{input_data.thread_id}:{input_data.turn_id}"
            turn = self._turns.setdefault(key, SimpleNamespace(notification_count=0))
            turn.notification_count += 3
        return await super().progress_by_label(input_data)


@pytest.mark.asyncio
async def test_poll_survives_when_backend_streams_while_rpcs_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(super_agents_client_module, "TURN_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(
        super_agents_client_module, "TURN_POLL_FAILURE_BACKOFF_MAX_SECONDS", 0.02
    )
    monkeypatch.setattr(
        super_agents_client_module, "TURN_POLL_MAX_CONSECUTIVE_FAILURES", 2
    )
    # Six straight poll failures would blow through the give-up limit of 2,
    # but the notification stream keeps moving, so the client must keep
    # waiting and eventually deliver the answer.
    backend = FakeBusyStreamingBackend(failures_before_success=6)
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(tmp_path / "livekit-voice-route.json"),
        backend_client=backend,
    )

    result = await client.run_turn("capture the debug diagnostics")

    assert result["_livekit_turn_id"] == "turn-1"
    assert result["_livekit_speech_text"] == "The answer survived the poll timeouts."


@pytest.mark.asyncio
async def test_poll_gives_up_when_backend_stream_stalls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(super_agents_client_module, "TURN_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(
        super_agents_client_module, "TURN_POLL_FAILURE_BACKOFF_MAX_SECONDS", 0.02
    )
    monkeypatch.setattr(
        super_agents_client_module, "TURN_POLL_MAX_CONSECUTIVE_FAILURES", 2
    )
    # The stream stalls after two progress calls: a genuinely dead backend
    # must still hit the give-up limit instead of waiting forever.
    backend = FakeBusyStreamingBackend(failures_before_success=100, streaming_calls=2)
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(tmp_path / "livekit-voice-route.json"),
        backend_client=backend,
    )

    with pytest.raises(TimeoutError):
        await client.run_turn("capture the debug diagnostics")


def test_dispatcher_dedicated_endpoint_defaults_to_own_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openbase_coder_cli.codex_control_plane import (
        dispatcher_codex_app_server_endpoint,
    )

    endpoint = dispatcher_codex_app_server_endpoint({"CODEX_HOME": str(tmp_path)})
    assert endpoint.is_unix
    assert (
        endpoint.socket_path
        == tmp_path / "app-server-control-dispatcher" / "app-server-control.sock"
    )
    explicit = dispatcher_codex_app_server_endpoint(
        {"OPENBASE_DISPATCHER_APP_SERVER_URL": "unix:///tmp/other.sock"}
    )
    assert explicit.source == "dispatcher-explicit"
    assert explicit.socket_path == Path("/tmp/other.sock")


def test_dispatcher_route_uses_dedicated_endpoint_only_when_socket_is_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENBASE_DISPATCHER_APP_SERVER_URL", raising=False)
    monkeypatch.delenv("OPENBASE_DISPATCHER_DEDICATED_APP_SERVER", raising=False)
    backend = FakeSuperAgentsBackend()
    dispatcher = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(tmp_path / "livekit-voice-route.json"),
        backend_client=backend,
    )
    # Socket absent: fall back to the shared endpoint.
    assert dispatcher._dispatcher_dedicated_endpoint() is None

    socket_path = tmp_path / "app-server-control-dispatcher" / "app-server-control.sock"
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()
    assert dispatcher._dispatcher_dedicated_endpoint() == f"unix://{socket_path}"

    # The kill switch forces the shared endpoint even with the socket up.
    monkeypatch.setenv("OPENBASE_DISPATCHER_DEDICATED_APP_SERVER", "0")
    assert dispatcher._dispatcher_dedicated_endpoint() is None
    monkeypatch.delenv("OPENBASE_DISPATCHER_DEDICATED_APP_SERVER")

    # Transferred Super Agent routes never move off the shared endpoint.
    transferred = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=None,
        persist_thread=False,
        initial_thread_id="thread-x",
        backend_client=backend,
    )
    assert transferred._dispatcher_dedicated_endpoint() is None


class FakeSteerTimeoutBackend(FakeSuperAgentsBackend):
    def __init__(self) -> None:
        super().__init__()
        self.queued_turns: list[tuple[Any, dict[str, Any]]] = []
        self.steer_attempts = 0

    async def steer_by_label(
        self,
        input_data,
        prompt: str,
        turn_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.steer_attempts += 1
        raise TimeoutError("Timed out waiting for app-server response to turn/steer.")

    async def queue_turn_by_label(
        self,
        input_data,
        turn_input: dict[str, Any],
    ) -> dict[str, Any]:
        self.queued_turns.append((input_data, turn_input))
        return {"queued": True, "queuedId": "queued-1"}


@pytest.mark.asyncio
async def test_steer_timeout_queues_follow_up_instead_of_dropping(
    tmp_path: Path,
) -> None:
    backend = FakeSteerTimeoutBackend()
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(tmp_path / "livekit-voice-route.json"),
        backend_client=backend,
    )
    await client.prepare()
    client._active_turn_id = "turn-1"
    client._active_turn_prompt_hash = "previous-prompt"

    turn_id = await client.steer_active_turn("also rename the incident file")

    assert backend.steer_attempts == 1
    assert turn_id == "turn-1"
    assert len(backend.queued_turns) == 1
    assert backend.queued_turns[0][1]["prompt"] == "also rename the incident file"
