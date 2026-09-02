from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openbase_coder_cli.livekit_agent import (
    super_agents_client as super_agents_client_module,
)
from openbase_coder_cli.livekit_agent.super_agents_client import (
    SuperAgentsLiveKitClient,
    _configured_execution_backend,
    _extract_turn_id,
    _response_is_queued,
    _speech_text_from_progress,
)


class FakeSuperAgentsBackend:
    backend = "claude-agent-sdk"

    def __init__(self) -> None:
        self.started_threads: list[dict[str, Any]] = []
        self.started_turns: list[tuple[Any, dict[str, Any]]] = []
        self.progress_calls = 0
        self.permission_callback: Any | None = None

    def register_permission_callback(self, callback: Any) -> None:
        self.permission_callback = callback

    async def start_thread(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.started_threads.append(input_data)
        return {"threadId": "dispatcher-thread"}

    async def resume_by_label(self, input_data) -> dict[str, Any]:
        return {"threadId": input_data.thread_id}

    async def start_turn_by_label(
        self,
        input_data,
        turn_input: dict[str, Any],
    ) -> dict[str, Any]:
        self.started_turns.append((input_data, turn_input))
        return {"turnId": f"turn-{len(self.started_turns)}"}

    async def steer_by_label(
        self,
        input_data,
        prompt: str,
        turn_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"turnId": input_data.turn_id, "prompt": prompt}

    async def progress_by_label(self, input_data) -> dict[str, Any]:
        self.progress_calls += 1
        return {
            "status": "waiting",
            "threadId": input_data.thread_id,
            "turnId": input_data.turn_id,
            "lastUsefulMessage": "The dispatcher answer is ready.",
        }


class FakeCodexSuperAgentsBackend(FakeSuperAgentsBackend):
    backend = "codex"

    def __init__(self) -> None:
        super().__init__()
        self.started_direct_turns: list[dict[str, Any]] = []
        self.resumed_threads: list[dict[str, Any]] = []

    async def resume_thread(
        self,
        thread_id: str,
        *,
        label: str | None = None,
        agent_name: str | None = None,
        developer_instructions: str | None = None,
    ) -> dict[str, Any]:
        self.resumed_threads.append(
            {
                "thread_id": thread_id,
                "label": label,
                "agent_name": agent_name,
                "developer_instructions": developer_instructions,
            }
        )
        return {"threadId": thread_id}

    async def start_turn(self, turn_input: dict[str, Any]) -> dict[str, Any]:
        self.started_direct_turns.append(turn_input)
        return {"turnId": "direct-turn-1"}

    async def progress_by_label(self, input_data) -> dict[str, Any]:
        self.progress_calls += 1
        return {
            "status": "waiting",
            "threadId": input_data.thread_id,
            "turnId": input_data.turn_id,
            "summary": {
                "items": [
                    {
                        "type": "agentMessage",
                        "text": "The direct dispatcher answer is ready.",
                    }
                ]
            },
        }


def test_configured_execution_backend_prefers_installed_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from openbase_coder_cli import paths

    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=openbase_cloud\n", encoding="utf-8")
    monkeypatch.setattr(paths, "DEFAULT_ENV_FILE_PATH", env_file)

    environment_backend_calls: list[bool] = []

    def environment_backend() -> str:
        environment_backend_calls.append(True)
        return "codex"

    assert _configured_execution_backend(environment_backend) == "claude_code"
    assert environment_backend_calls == []


def test_configured_execution_backend_falls_back_for_unsupported_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from openbase_coder_cli import paths

    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=not-a-backend\n", encoding="utf-8")
    monkeypatch.setattr(paths, "DEFAULT_ENV_FILE_PATH", env_file)

    assert _configured_execution_backend(lambda: "codex") == "codex"


class FakeQueuedSuperAgentsBackend(FakeSuperAgentsBackend):
    def __init__(self) -> None:
        super().__init__()
        self.progress_inputs: list[Any] = []
        self.latest_progress_calls = 0

    async def start_turn_by_label(
        self,
        input_data,
        turn_input: dict[str, Any],
    ) -> dict[str, Any]:
        self.started_turns.append((input_data, turn_input))
        return {
            "queued": True,
            "threadId": "dispatcher-thread",
            "queueDepth": 1,
            "item": {
                "id": "q_queued-follow-up",
                "threadId": "dispatcher-thread",
                "status": "queued",
            },
        }

    async def progress_by_label(self, input_data) -> dict[str, Any]:
        self.progress_calls += 1
        self.progress_inputs.append(input_data)
        if input_data.turn_id == "q_queued-follow-up":
            raise AssertionError("LiveKit must not poll queued item ids as turn ids")
        if input_data.turn_id == "turn-2":
            return {
                "status": "waiting",
                "threadId": input_data.thread_id,
                "turnId": "turn-2",
                "summary": {
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": "The queued dispatcher answer is ready.",
                        }
                    ]
                },
            }
        self.latest_progress_calls += 1
        return {
            "status": "running",
            "threadId": input_data.thread_id,
            "turnId": "turn-1" if self.latest_progress_calls == 1 else "turn-2",
        }


class FakeExternallyActiveSuperAgentsBackend(FakeSuperAgentsBackend):
    def __init__(self) -> None:
        super().__init__()
        self.resolved_labels: list[Any] = []
        self.steered: list[tuple[Any, str]] = []
        self.steer_turn_inputs: list[dict[str, Any] | None] = []

    async def resolve_label(self, input_data) -> dict[str, Any]:
        self.resolved_labels.append(input_data)
        return {
            "status": "running",
            "threadId": input_data.thread_id,
            "turnId": "active-turn-1",
        }

    async def steer_by_label(
        self,
        input_data,
        prompt: str,
        turn_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.steered.append((input_data, prompt))
        self.steer_turn_inputs.append(turn_input)
        return {"turnId": input_data.turn_id, "prompt": prompt}

    async def progress_by_label(self, input_data) -> dict[str, Any]:
        self.progress_calls += 1
        return {
            "status": "waiting",
            "threadId": input_data.thread_id,
            "turnId": input_data.turn_id,
            "summary": {
                "items": [
                    {
                        "type": "agentMessage",
                        "text": "The steered dispatcher answer is ready.",
                    }
                ]
            },
        }


class FakeSlowStartSuperAgentsBackend(FakeSuperAgentsBackend):
    def __init__(self) -> None:
        super().__init__()
        self.start_called = asyncio.Event()
        self.release_start = asyncio.Event()
        self.steered: list[tuple[Any, str]] = []
        self.steer_turn_inputs: list[dict[str, Any] | None] = []

    async def start_turn_by_label(
        self,
        input_data,
        turn_input: dict[str, Any],
    ) -> dict[str, Any]:
        self.started_turns.append((input_data, turn_input))
        self.start_called.set()
        await self.release_start.wait()
        return {"turnId": "turn-1"}

    async def steer_by_label(
        self,
        input_data,
        prompt: str,
        turn_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.steered.append((input_data, prompt))
        self.steer_turn_inputs.append(turn_input)
        return {"turnId": input_data.turn_id, "prompt": prompt}

    async def progress_by_label(self, input_data) -> dict[str, Any]:
        self.progress_calls += 1
        return {
            "status": "waiting",
            "threadId": input_data.thread_id,
            "turnId": input_data.turn_id,
            "summary": {
                "items": [
                    {
                        "type": "agentMessage",
                        "text": "The interrupted turn accepted steering.",
                    }
                ]
            },
        }


class FakeLongRunningSuperAgentsBackend(FakeSuperAgentsBackend):
    def __init__(self) -> None:
        super().__init__()
        self.progress_called = asyncio.Event()
        self.release_progress = asyncio.Event()
        self.steered: list[tuple[Any, str]] = []
        self.steer_turn_inputs: list[dict[str, Any] | None] = []

    async def start_turn_by_label(
        self,
        input_data,
        turn_input: dict[str, Any],
    ) -> dict[str, Any]:
        self.started_turns.append((input_data, turn_input))
        return {"turnId": f"turn-{len(self.started_turns)}"}

    async def steer_by_label(
        self,
        input_data,
        prompt: str,
        turn_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.steered.append((input_data, prompt))
        self.steer_turn_inputs.append(turn_input)
        return {"turnId": input_data.turn_id, "prompt": prompt}

    async def progress_by_label(self, input_data) -> dict[str, Any]:
        self.progress_calls += 1
        if not self.started_turns:
            return {
                "status": "completed",
                "threadId": input_data.thread_id,
            }
        self.progress_called.set()
        await self.release_progress.wait()
        return {
            "status": "waiting",
            "threadId": input_data.thread_id,
            "turnId": input_data.turn_id,
            "summary": {
                "items": [
                    {
                        "type": "agentMessage",
                        "text": "The proactively steered turn is ready.",
                    }
                ]
            },
        }


class FakeStaleActiveSuperAgentsBackend(FakeSuperAgentsBackend):
    def __init__(self) -> None:
        super().__init__()
        self.steered: list[tuple[Any, str]] = []
        self.steer_turn_inputs: list[dict[str, Any] | None] = []

    async def resolve_label(self, input_data) -> dict[str, Any]:
        return {
            "status": "running",
            "threadId": input_data.thread_id,
            "turnId": "stale-turn",
        }

    async def steer_by_label(
        self,
        input_data,
        prompt: str,
        turn_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.steered.append((input_data, prompt))
        self.steer_turn_inputs.append(turn_input)
        return {"turnId": input_data.turn_id, "prompt": prompt}

    async def progress_by_label(self, input_data) -> dict[str, Any]:
        self.progress_calls += 1
        if input_data.turn_id == "stale-turn":
            return {
                "status": "completed",
                "threadId": input_data.thread_id,
                "turnId": "stale-turn",
            }
        return {
            "status": "waiting",
            "threadId": input_data.thread_id,
            "turnId": input_data.turn_id,
            "summary": {
                "items": [
                    {
                        "type": "agentMessage",
                        "text": "The replacement turn is ready.",
                    }
                ]
            },
        }


@pytest.mark.asyncio
async def test_super_agents_livekit_client_creates_thread_and_turn_through_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    config_path = tmp_path / "dispatcher-config.json"
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "claude_code")
    config_path.write_text(
        json.dumps(
            {
                "backend_models": {
                    "claude_code": {
                        "dispatcher": "haiku",
                        "super_agents": "opus",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        developer_instructions="dispatcher instructions",
        dispatcher_config_path=config_path,
        backend_client=backend,
    )

    thread_id = await client.prepare()
    result = await client.run_turn(
        "hello",
        developer_instructions="voice instructions",
    )

    assert thread_id == "dispatcher-thread"
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["dispatcher_thread_id"]
        == "dispatcher-thread"
    )
    assert backend.started_threads[0]["label"] == "dispatcher"
    assert backend.started_threads[0]["cwd"] == "/tmp/project"
    assert backend.started_threads[0]["model"] == "haiku"
    assert backend.started_turns[0][0].thread_id == "dispatcher-thread"
    assert backend.started_turns[0][1]["prompt"] == "hello"
    assert backend.started_turns[0][1]["model"] == "haiku"
    assert (
        "dispatcher instructions"
        in backend.started_turns[0][1]["developerInstructions"]
    )
    assert "voice instructions" in backend.started_turns[0][1]["developerInstructions"]
    assert result["_livekit_turn_id"] == "turn-1"
    assert result["_livekit_speech_text"] == "The dispatcher answer is ready."
    assert "fresh" not in backend.started_threads[0]


@pytest.mark.asyncio
async def test_super_agents_livekit_client_fresh_thread_requests_fresh_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "claude_code")
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
        fresh_thread=True,
    )

    await client.prepare()

    assert backend.started_threads[0]["fresh"] is True
    # The flag is consumed by the first start; later starts on the same
    # client must not retire the thread they just created.
    client._thread_id = None
    client._thread_loaded = False
    state_path.unlink()
    await client.prepare()
    assert "fresh" not in backend.started_threads[1]


@pytest.mark.asyncio
async def test_super_agents_livekit_client_starts_codex_turn_by_thread_id(
    tmp_path: Path,
) -> None:
    backend = FakeCodexSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )

    result = await client.run_turn("hello")

    assert backend.started_turns == []
    assert backend.started_direct_turns[0]["threadId"] == "dispatcher-thread"
    assert backend.started_direct_turns[0]["prompt"] == "hello"
    assert result["_livekit_turn_id"] == "direct-turn-1"
    assert result["_livekit_speech_text"] == "The direct dispatcher answer is ready."


@pytest.mark.asyncio
async def test_super_agents_livekit_client_waits_for_queued_turn_to_start(
    tmp_path: Path,
) -> None:
    backend = FakeQueuedSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )

    result = await client.run_turn("hello")

    assert result["_livekit_turn_id"] == "turn-2"
    assert result["_livekit_speech_text"] == "The queued dispatcher answer is ready."
    assert [input_data.turn_id for input_data in backend.progress_inputs] == [
        None,
        None,
        "turn-2",
    ]


@pytest.mark.asyncio
async def test_super_agents_livekit_client_steers_backend_active_turn(
    tmp_path: Path,
) -> None:
    backend = FakeExternallyActiveSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )

    result = await client.run_turn("please adjust that")

    assert backend.started_turns == []
    assert len(backend.steered) == 1
    steer_input, prompt = backend.steered[0]
    assert steer_input.turn_id == "active-turn-1"
    assert prompt == "please adjust that"
    assert result["_livekit_turn_id"] == "active-turn-1"
    assert result["_livekit_speech_text"] == "The steered dispatcher answer is ready."


@pytest.mark.asyncio
async def test_super_agents_livekit_client_preserves_started_turn_after_cancellation(
    tmp_path: Path,
) -> None:
    backend = FakeSlowStartSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )

    first = asyncio.create_task(client.run_turn("write about strawberries"))
    await backend.start_called.wait()
    first.cancel()
    backend.release_start.set()

    with pytest.raises(asyncio.CancelledError):
        await first

    result = await client.run_turn("change it to blueberries")

    assert len(backend.started_turns) == 1
    assert len(backend.steered) == 1
    steer_input, prompt = backend.steered[0]
    assert steer_input.turn_id == "turn-1"
    assert prompt == "change it to blueberries"
    assert result["_livekit_turn_id"] == "turn-1"
    assert result["_livekit_speech_text"] == "The interrupted turn accepted steering."


@pytest.mark.asyncio
async def test_super_agents_livekit_client_preserves_completed_speech_after_cancellation(
    tmp_path: Path,
) -> None:
    backend = FakeLongRunningSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )
    handed_off: list[tuple[str, str]] = []

    def handle_completed(_client, turn_id: str, speech: str) -> None:
        assert client.claim_speech(turn_id)
        handed_off.append((turn_id, speech))

    client.set_orphaned_result_handler(handle_completed)

    first = asyncio.create_task(client.run_turn("find the old conversation"))
    await backend.progress_called.wait()
    first.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first

    backend.release_progress.set()
    await asyncio.sleep(0)

    assert await client.steer_active_turn("are you there") is None
    result = await client.run_turn("are you there")

    assert len(backend.started_turns) == 2
    assert backend.steered == []
    assert handed_off == [
        ("turn-1", "The proactively steered turn is ready."),
    ]
    assert result["_livekit_turn_id"] == "turn-2"
    assert result["_livekit_speech_text"] == "The proactively steered turn is ready."


class FakeFlakyProgressSuperAgentsBackend(FakeSuperAgentsBackend):
    def __init__(self, failures_before_success: int) -> None:
        super().__init__()
        self.failures_before_success = failures_before_success
        self.steered: list[tuple[Any, str]] = []

    async def steer_by_label(
        self,
        input_data,
        prompt: str,
        turn_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.steered.append((input_data, prompt))
        return {"turnId": input_data.turn_id, "prompt": prompt}

    async def progress_by_label(self, input_data) -> dict[str, Any]:
        self.progress_calls += 1
        if self.progress_calls <= self.failures_before_success:
            raise TimeoutError(
                "Timed out waiting for app-server response to thread/read."
            )
        return {
            "status": "waiting",
            "threadId": input_data.thread_id,
            "turnId": input_data.turn_id,
            "summary": {
                "items": [
                    {
                        "type": "agentMessage",
                        "text": "The answer survived the poll timeouts.",
                    }
                ]
            },
        }


class FakeDelayedUsefulMessageBackend(FakeSuperAgentsBackend):
    async def progress_by_label(self, input_data) -> dict[str, Any]:
        self.progress_calls += 1
        base = {
            "status": "completed",
            "threadId": input_data.thread_id,
            "turnId": input_data.turn_id,
            "lastObservedState": "Claude Code response completed.",
            "turn": {
                "turnId": input_data.turn_id,
                "status": "completed",
                "promptPreview": "What's the capital of Alaska?",
            },
            "turns": [
                {
                    "turnId": input_data.turn_id,
                    "status": "completed",
                    "promptPreview": "What's the capital of Alaska?",
                }
            ],
        }
        if self.progress_calls >= 2:
            base["turn"]["lastUsefulMessage"] = "Juneau."
            base["turns"][0]["lastUsefulMessage"] = "Juneau."
        return base


@pytest.mark.asyncio
async def test_super_agents_livekit_client_survives_transient_poll_timeouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(super_agents_client_module, "TURN_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(
        super_agents_client_module, "TURN_POLL_FAILURE_BACKOFF_MAX_SECONDS", 0.02
    )
    backend = FakeFlakyProgressSuperAgentsBackend(failures_before_success=3)
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )

    result = await client.run_turn("find the old conversation")

    assert backend.progress_calls == 4
    assert result["_livekit_turn_id"] == "turn-1"
    assert result["_livekit_speech_text"] == "The answer survived the poll timeouts."


@pytest.mark.asyncio
async def test_super_agents_livekit_client_waits_for_completed_turn_speech(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(super_agents_client_module, "TURN_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(
        super_agents_client_module,
        "TURN_POLL_COMPLETED_EMPTY_SPEECH_GRACE_SECONDS",
        1.0,
    )
    backend = FakeDelayedUsefulMessageBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )

    result = await client.run_turn("What's the capital of Alaska?")

    assert backend.progress_calls == 2
    assert result["_livekit_turn_id"] == "turn-1"
    assert result["_livekit_speech_text"] == "Juneau."


@pytest.mark.asyncio
async def test_super_agents_livekit_client_recovers_after_poll_gave_up(
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
    # The first progress call happens before the turn starts (latest-turn
    # lookup) and is swallowed there; the next two are wait-poll failures.
    backend = FakeFlakyProgressSuperAgentsBackend(failures_before_success=3)
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )

    with pytest.raises(TimeoutError):
        await client.run_turn("find the old conversation")

    # The backend turn is preserved so the next utterance can rejoin it and
    # the finished answer still gets spoken.
    result = await client.run_turn("are you there")

    assert len(backend.started_turns) == 1
    assert result["_livekit_turn_id"] == "turn-1"
    assert result["_livekit_speech_text"] == "The answer survived the poll timeouts."


@pytest.mark.asyncio
async def test_super_agents_livekit_client_delivers_orphaned_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        super_agents_client_module, "ORPHANED_RESULT_GRACE_SECONDS", 0.01
    )
    backend = FakeLongRunningSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )
    orphaned: list[tuple[str, str]] = []

    def handler(source_client, turn_id: str, speech_text: str) -> None:
        assert source_client is client
        if client.claim_speech(turn_id):
            orphaned.append((turn_id, speech_text))

    client.set_orphaned_result_handler(handler)

    first = asyncio.create_task(client.run_turn("find the old conversation"))
    await backend.progress_called.wait()
    assert client.has_pending_voice_answer()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert client.has_pending_voice_answer()

    backend.release_progress.set()
    await asyncio.sleep(0.1)

    assert orphaned == [("turn-1", "The proactively steered turn is ready.")]
    assert not client.has_pending_voice_answer()

    # The delivered answer must not be re-joined; the next utterance starts a
    # fresh turn.
    result = await client.run_turn("next question")
    assert len(backend.started_turns) == 2
    assert result["_livekit_turn_id"] == "turn-2"


@pytest.mark.asyncio
async def test_orphan_spoken_turn_suppresses_duplicate_prompt_fresh_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Fable incident: a correction is steered into a running turn, the
    # waiter dies, orphan delivery speaks the finished answer — and then a
    # twin/repeat of the correction arrives as a fresh prompt. It must not
    # start another backend turn that answers with the same gist again.
    monkeypatch.setattr(
        super_agents_client_module, "ORPHANED_RESULT_GRACE_SECONDS", 0.01
    )
    backend = FakeLongRunningSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )
    orphaned: list[str] = []

    def handler(_client, turn_id: str, _speech: str) -> None:
        if client.claim_speech(turn_id):
            orphaned.append(turn_id)

    client.set_orphaned_result_handler(handler)

    first = asyncio.create_task(client.run_turn("what was fable working on"))
    await backend.progress_called.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    steered_turn = await client.steer_active_turn("im not like what did fable do")
    assert steered_turn == "turn-1"

    backend.release_progress.set()
    await asyncio.sleep(0.1)
    assert orphaned == ["turn-1"]

    # A formatted STT twin (or the user restating) of the steered correction.
    result = await client.run_turn("I'm not, like, what did Fable do?")
    assert result["_livekit_speech_text"] == ""
    assert result["_livekit_turn_id"] == "turn-1"
    assert len(backend.started_turns) == 1

    # A twin of the original prompt is likewise already answered.
    result = await client.run_turn("What was Fable working on?")
    assert result["_livekit_speech_text"] == ""
    assert len(backend.started_turns) == 1

    # Genuinely new content still gets a fresh backend turn.
    result = await client.run_turn("start a new agent for the report")
    assert len(backend.started_turns) == 2


@pytest.mark.asyncio
async def test_super_agents_livekit_client_hands_off_completed_result_before_new_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        super_agents_client_module, "ORPHANED_RESULT_GRACE_SECONDS", 0.01
    )
    backend = FakeLongRunningSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )
    orphaned: list[str] = []

    def handle_completed(_client, turn_id, _speech) -> None:
        assert client.claim_speech(turn_id)
        orphaned.append(turn_id)

    client.set_orphaned_result_handler(handle_completed)

    first = asyncio.create_task(client.run_turn("find the old conversation"))
    await backend.progress_called.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    backend.release_progress.set()
    await asyncio.sleep(0)

    # The old result is handed off, and the new utterance gets its own turn.
    result = await client.run_turn("are you there")
    assert client.claim_speech(result["_livekit_turn_id"])
    await asyncio.sleep(0.1)

    assert orphaned == ["turn-1"]
    assert result["_livekit_turn_id"] == "turn-2"


class FakeSteerRejectedSuperAgentsBackend(FakeSuperAgentsBackend):
    def __init__(self) -> None:
        super().__init__()
        self.queued_turns: list[tuple[Any, dict[str, Any]]] = []

    async def steer_by_label(
        self,
        input_data,
        prompt: str,
        turn_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("turn cannot accept steering")

    async def queue_turn_by_label(
        self,
        input_data,
        turn_input: dict[str, Any],
    ) -> dict[str, Any]:
        self.queued_turns.append((input_data, turn_input))
        return {"turnId": "turn-2", "queued": False}


@pytest.mark.asyncio
async def test_super_agents_livekit_client_submits_rejected_steer_to_queue_endpoint(
    tmp_path: Path,
) -> None:
    backend = FakeSteerRejectedSuperAgentsBackend()
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(tmp_path / "livekit-voice-route.json"),
        backend_client=backend,
    )
    await client.prepare()
    client._active_turn_id = "turn-1"
    client._active_turn_prompt_hash = "previous-prompt"

    result = await client.run_turn("do this after the current work")

    assert result["_livekit_turn_id"] == "turn-2"
    assert len(backend.queued_turns) == 1
    assert backend.queued_turns[0][1]["prompt"] == "do this after the current work"


@pytest.mark.asyncio
async def test_super_agents_livekit_client_proactively_steers_active_turn(
    tmp_path: Path,
) -> None:
    backend = FakeLongRunningSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )

    first = asyncio.create_task(client.run_turn("write about strawberries"))
    await backend.progress_called.wait()

    turn_id = await client.steer_active_turn("stop and write about blueberries")
    follow_up = asyncio.create_task(client.run_turn("stop and write about blueberries"))
    await asyncio.sleep(0)

    assert turn_id == "turn-1"
    assert len(backend.started_turns) == 1
    assert len(backend.steered) == 1
    steer_input, prompt = backend.steered[0]
    assert steer_input.turn_id == "turn-1"
    assert prompt == "stop and write about blueberries"

    backend.release_progress.set()
    first_result = await first
    follow_up_result = await follow_up

    assert len(backend.started_turns) == 1
    assert len(backend.steered) == 1
    assert first_result["_livekit_turn_id"] == "turn-1"
    assert follow_up_result["_livekit_turn_id"] == "turn-1"


@pytest.mark.asyncio
async def test_super_agents_livekit_client_passes_dispatcher_reasoning_to_steer(
    tmp_path: Path,
) -> None:
    class CodexLongRunningBackend(FakeLongRunningSuperAgentsBackend):
        backend = "codex"

    backend = CodexLongRunningBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    dispatcher_config_path = tmp_path / "dispatcher-config.json"
    dispatcher_config_path.write_text(
        json.dumps({"dispatcher_reasoning_effort": "low"}),
        encoding="utf-8",
    )
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        dispatcher_config_path=dispatcher_config_path,
        backend_client=backend,
    )

    first = asyncio.create_task(client.run_turn("write about strawberries"))
    await backend.progress_called.wait()

    turn_id = await client.steer_active_turn("stop and write about blueberries")

    backend.release_progress.set()
    await first

    assert turn_id == "turn-1"
    assert backend.steer_turn_inputs[0]["reasoningEffort"] == "low"
    assert backend.started_turns[0][1]["serviceTier"] == "standard"


@pytest.mark.asyncio
async def test_super_agents_livekit_client_forwards_reasoning_without_tier_on_claude(
    tmp_path: Path,
) -> None:
    backend = FakeLongRunningSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    dispatcher_config_path = tmp_path / "dispatcher-config.json"
    dispatcher_config_path.write_text(
        json.dumps(
            {
                "dispatcher_reasoning_effort": "low",
                "super_agents_reasoning_effort": "xhigh",
            }
        ),
        encoding="utf-8",
    )
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        dispatcher_config_path=dispatcher_config_path,
        backend_client=backend,
    )

    first = asyncio.create_task(client.run_turn("write about strawberries"))
    await backend.progress_called.wait()

    turn_id = await client.steer_active_turn("stop and write about blueberries")

    backend.release_progress.set()
    await first

    assert turn_id == "turn-1"
    assert backend.started_turns[0][1]["reasoningEffort"] == "low"
    assert backend.steer_turn_inputs[0]["reasoningEffort"] == "low"
    assert "serviceTier" not in backend.started_turns[0][1]


@pytest.mark.asyncio
async def test_super_agents_livekit_client_proactive_steer_does_not_start_turn(
    tmp_path: Path,
) -> None:
    backend = FakeSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )

    turn_id = await client.steer_active_turn("hello")

    assert turn_id is None
    assert backend.started_turns == []


@pytest.mark.asyncio
async def test_super_agents_livekit_client_does_not_steer_stale_active_turn(
    tmp_path: Path,
) -> None:
    backend = FakeStaleActiveSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )

    result = await client.run_turn("start fresh")

    assert backend.steered == []
    assert len(backend.started_turns) == 1
    assert result["_livekit_turn_id"] == "turn-1"
    assert result["_livekit_speech_text"] == "The replacement turn is ready."


@pytest.mark.asyncio
async def test_super_agents_livekit_client_resumes_codex_thread_by_id(
    tmp_path: Path,
) -> None:
    backend = FakeCodexSuperAgentsBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    state_path.write_text(
        json.dumps({"dispatcher_thread_id": "canonical-dispatcher-thread"}),
        encoding="utf-8",
    )
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        initial_thread_id="stale-dispatcher-thread",
        backend_client=backend,
    )

    thread_id = await client.prepare()

    assert thread_id == "canonical-dispatcher-thread"
    assert backend.resumed_threads == [
        {
            "thread_id": "canonical-dispatcher-thread",
            "label": "dispatcher",
            "agent_name": None,
            "developer_instructions": "Super Agent thread name: dispatcher",
        }
    ]
    assert backend.started_threads == []
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["dispatcher_thread_id"]
        == "canonical-dispatcher-thread"
    )


def test_super_agents_livekit_client_answers_mcp_elicitations(tmp_path: Path) -> None:
    backend = FakeCodexSuperAgentsBackend()
    SuperAgentsLiveKitClient(
        cwd=str(tmp_path),
        state_path=str(tmp_path / "livekit-voice-route.json"),
        backend_client=backend,
    )

    assert backend.permission_callback is not None
    accepted = backend.permission_callback(
        SimpleNamespace(
            method="mcpServer/elicitation/request",
            params={"serverName": "super_agents"},
        )
    )
    declined = backend.permission_callback(
        SimpleNamespace(
            method="mcpServer/elicitation/request",
            params={"serverName": "chrome"},
        )
    )

    assert accepted == {"action": "accept", "content": None, "_meta": None}
    assert declined == {"action": "decline", "content": None, "_meta": None}


def test_speech_text_from_progress_uses_agent_message_text() -> None:
    progress = {
        "summary": {
            "items": [
                {
                    "type": "agentMessage",
                    "text": "Here is the useful answer.",
                }
            ]
        }
    }

    assert _speech_text_from_progress(progress) == "Here is the useful answer."


def test_speech_text_from_progress_ignores_agent_message_type_label() -> None:
    progress = {
        "summary": {
            "items": [
                {
                    "type": "agentMessage",
                }
            ]
        },
        "turn": {
            "items": [
                {
                    "type": "agentMessage",
                    "phase": "final",
                }
            ]
        },
    }

    assert _speech_text_from_progress(progress) == ""


def test_speech_text_from_progress_ignores_spaced_agent_message_label() -> None:
    progress = {
        "summary": {
            "lastUsefulMessage": "agent message.",
        }
    }

    assert _speech_text_from_progress(progress) == ""


def test_speech_text_from_completed_progress_ignores_metadata_only_items() -> None:
    progress = {
        "status": "completed",
        "threadId": "thread-1",
        "turnId": "turn-1",
        "summary": {
            "id": "turn-1",
            "status": "completed",
            "reasoningEffort": "low",
            "startedAt": "2026-08-05T13:23:30Z",
            "completedAt": "2026-08-05T13:23:39Z",
            "eventCount": 4,
            "pendingRequestCount": 0,
            "items": [
                {
                    "type": "userMessage",
                    "id": "item-1",
                    "role": "user",
                },
                {
                    "type": "agentMessage",
                    "id": "item-2",
                    "role": "assistant",
                    "phase": "final",
                    "status": "completed",
                },
            ],
        },
        "turn": {
            "id": "turn-1",
            "status": "completed",
            "items": [
                {
                    "type": "agentMessage",
                    "id": "item-2",
                    "phase": "final",
                }
            ],
        },
        "recentTurns": [
            {
                "id": "turn-1",
                "status": "completed",
            }
        ],
    }

    assert _speech_text_from_progress(progress) == ""


def test_speech_text_from_progress_ignores_turn_ids() -> None:
    progress = {
        "summary": {
            "lastUsefulMessage": "019edae2 e304 77a3 9ddb 470ed17e64f7.",
        },
        "turn": {
            "id": "019edae2-e304-77a3-9ddb-470ed17e64f7",
            "status": "completed",
        },
    }

    assert _speech_text_from_progress(progress) == ""


def test_extract_turn_id_ignores_queued_item_id() -> None:
    payload = {
        "queued": True,
        "item": {
            "id": "q_queued-follow-up",
            "status": "queued",
        },
    }

    assert _extract_turn_id(payload) is None


def test_top_level_queue_item_id_is_queued_response() -> None:
    payload = {"turnId": "q_queued-follow-up", "status": "queued"}

    assert _response_is_queued(payload) is True
    assert _extract_turn_id(payload) is None


def test_speech_text_from_progress_ignores_user_message_text() -> None:
    progress = {
        "summary": {
            "items": [
                {
                    "type": "userMessage",
                    "content": [
                        {
                            "type": "text",
                            "text": "hey are you there",
                        }
                    ],
                }
            ],
            "lastUsefulMessage": "hey are you there",
        },
        "turn": {
            "items": [
                {
                    "type": "userMessage",
                    "content": [
                        {
                            "type": "text",
                            "text": "can you hear me",
                        }
                    ],
                }
            ]
        },
    }

    assert _speech_text_from_progress(progress) == ""


def test_speech_text_from_progress_uses_short_assistant_answer() -> None:
    progress = {
        "summary": {
            "lastUsefulMessage": "What is the capital of Alaska?",
        },
        "turn": {
            "items": [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "What is the capital of Alaska?",
                            }
                        ],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Juneau.",
                            }
                        ],
                    },
                },
            ]
        },
    }

    assert _speech_text_from_progress(progress) == "Juneau."


def test_speech_text_from_progress_rejects_prompt_preview_readback() -> None:
    progress = {
        "summary": {
            "lastUsefulMessage": "Your answer was not actually spoken.",
        },
        "turn": {
            "promptPreview": "Your answer was not actually spoken.",
            "items": [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                    },
                }
            ],
        },
    }

    assert _speech_text_from_progress(progress) == ""


def test_speech_text_from_progress_does_not_speak_observed_state() -> None:
    progress = {
        "status": "completed",
        "lastObservedState": "Claude Code response completed.",
        "turn": {
            "turnId": "turn-1",
            "status": "completed",
            "promptPreview": "What's the capital of Alaska?",
        },
        "turns": [
            {
                "turnId": "turn-1",
                "status": "completed",
                "promptPreview": "What's the capital of Alaska?",
            }
        ],
    }

    assert _speech_text_from_progress(progress) == ""


def test_speech_text_from_progress_ignores_timestamp_artifact() -> None:
    progress = {
        "turns": [
            {
                "turnId": "turn-1",
                "status": "cancelled",
                "createdAt": "2026-08-05T22:55:38.399Z",
            }
        ]
    }

    assert _speech_text_from_progress(progress) == ""


def test_speech_text_from_progress_prefers_scoped_turn_over_session_preview() -> None:
    progress = {
        "status": "completed",
        "lastUsefulMessage": "A stale session-level preview.",
        "turn": {
            "turnId": "turn-2",
            "status": "completed",
            "lastUsefulMessage": "The requested turn answer.",
        },
        "turns": [
            {
                "turnId": "turn-2",
                "status": "completed",
                "lastUsefulMessage": "The requested turn answer.",
            }
        ],
    }

    assert _speech_text_from_progress(progress) == "The requested turn answer."


def test_backend_auth_failure_warning_debounced_and_backend_specific(
    monkeypatch, caplog
) -> None:
    import logging

    monkeypatch.setattr(
        super_agents_client_module, "_last_backend_auth_warn_monotonic", None
    )
    caplog.set_level(logging.WARNING)

    assert (
        super_agents_client_module._flag_backend_auth_failure(
            "Failed to authenticate: OAuth session expired and could not be refreshed",
            backend="claude_code",
            turn_id="t_1",
        )
        is True
    )
    # The human-readable WARNING fires once and names the Claude-specific fix.
    assert caplog.text.count("surfaced in a spoken answer") == 1
    assert "claude login" in caplog.text

    # Debounced: an immediate repeat must not emit a second WARNING.
    super_agents_client_module._flag_backend_auth_failure(
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        backend="claude_code",
        turn_id="t_2",
    )
    assert caplog.text.count("surfaced in a spoken answer") == 1


def test_backend_auth_failure_always_emits_error_marker(monkeypatch, caplog) -> None:
    import logging

    # Warning is debounced already, but the structured ERROR marker must fire on
    # every failed turn so an auth failure never looks like a completed success.
    monkeypatch.setattr(
        super_agents_client_module, "_last_backend_auth_warn_monotonic", 10_000_000.0
    )
    caplog.set_level(logging.ERROR)

    for turn_id in ("t_1", "t_2"):
        super_agents_client_module._flag_backend_auth_failure(
            "Not logged in · Please run /login.",
            backend="openbase_cloud",
            turn_id=turn_id,
        )

    assert caplog.text.count("stage=voice_turn_backend_auth_failure") == 2
    # Codex/openbase_cloud remediation must NOT tell the user to run `claude login`.
    assert "claude login" not in caplog.text
    assert "machine token" in caplog.text


def test_backend_auth_failure_ignores_normal_speech(monkeypatch, caplog) -> None:
    import logging

    monkeypatch.setattr(
        super_agents_client_module, "_last_backend_auth_warn_monotonic", None
    )
    caplog.set_level(logging.WARNING)

    assert (
        super_agents_client_module._flag_backend_auth_failure(
            "The build passed and I pushed the fix.", backend="codex"
        )
        is False
    )
    assert super_agents_client_module._flag_backend_auth_failure("") is False

    assert "auth failure" not in caplog.text
    assert "voice_turn_backend_auth_failure" not in caplog.text


@pytest.mark.asyncio
async def test_preflight_warns_for_logged_out_claude_backend(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    import logging

    backend = FakeSuperAgentsBackend()
    backend.backend = "claude_code"
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(
        super_agents_client_module,
        "verified_claude_auth_status",
        lambda: SimpleNamespace(
            logged_in=False,
            raw_output="Not logged in · Please run /login",
            returncode=0,
        ),
    )

    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(tmp_path / "state.json"),
        backend_client=backend,
    )
    result = await client.run_turn("hello dispatch")

    assert result["_livekit_turn_id"] == "turn-1"
    assert "claude login" in caplog.text


@pytest.mark.asyncio
async def test_preflight_quiet_when_claude_logged_in(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    import logging

    backend = FakeSuperAgentsBackend()
    backend.backend = "claude_code"
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(
        super_agents_client_module,
        "verified_claude_auth_status",
        lambda: SimpleNamespace(
            logged_in=True, raw_output='{"loggedIn": true}', returncode=0
        ),
    )

    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(tmp_path / "state.json"),
        backend_client=backend,
    )
    result = await client.run_turn("hello dispatch")

    assert result["_livekit_turn_id"] == "turn-1"
    assert "claude login" not in caplog.text


@pytest.mark.asyncio
async def test_preflight_heal_skipped_for_non_claude_backends(
    monkeypatch, tmp_path: Path
) -> None:
    def _no_status():
        raise AssertionError("auth status must not run for non-claude backends")

    monkeypatch.setattr(
        super_agents_client_module, "verified_claude_auth_status", _no_status
    )

    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(tmp_path / "state.json"),
        backend_client=FakeSuperAgentsBackend(),
    )
    await client.prepare()


class FakeEventDrivenBackend(FakeSuperAgentsBackend):
    """Backend that pushes turn completion via wait_for_turn_update instead of
    forcing the poll loop to re-query on a fixed interval."""

    def __init__(self) -> None:
        super().__init__()
        self._completed = False
        self.wait_calls = 0

    async def progress_by_label(self, input_data) -> dict[str, Any]:
        self.progress_calls += 1
        if not self._completed:
            return {
                "status": "running",
                "threadId": input_data.thread_id,
                "turnId": input_data.turn_id,
            }
        return {
            "status": "completed",
            "threadId": input_data.thread_id,
            "turnId": input_data.turn_id,
            "lastUsefulMessage": "The event driven answer is ready.",
        }

    async def wait_for_turn_update(
        self, thread_id: str, turn_id: str, timeout: float
    ) -> None:
        # Stand in for a pushed turn/completed notification arriving.
        self.wait_calls += 1
        self._completed = True


@pytest.mark.asyncio
async def test_super_agents_livekit_client_awaits_pushed_turn_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A big fixed interval would make this test hang for seconds if the loop
    # fell back to sleeping; awaiting the push must return promptly instead.
    monkeypatch.setattr(super_agents_client_module, "TURN_POLL_INTERVAL_SECONDS", 30.0)
    backend = FakeEventDrivenBackend()
    state_path = tmp_path / "livekit-voice-route.json"
    client = SuperAgentsLiveKitClient(
        cwd="/tmp/project",
        state_path=str(state_path),
        backend_client=backend,
    )

    result = await asyncio.wait_for(client.run_turn("what's the answer?"), timeout=2.0)

    # The loop awaited the pushed completion (wait_calls) rather than sleeping
    # out the 30s fixed interval, and re-read progress once the push landed.
    assert backend.wait_calls == 1
    assert backend.progress_calls >= 2
    assert result["_livekit_speech_text"] == "The event driven answer is ready."
