"""Direct voice mode: calls talk to a fresh ordinary thread, no dispatcher."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openbase_coder_cli import dispatcher_config
from openbase_coder_cli.direct_voice_instructions import (
    DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS,
    DIRECT_VOICE_MODE_BUILTIN_DEVELOPER_INSTRUCTIONS,
)
from openbase_coder_cli.livekit_agent import config as livekit_config
from openbase_coder_cli.livekit_agent.voice_routing import (
    DirectVoiceHome,
    LiveKitVoiceRouter,
)


class _FakeHomeClient:
    def __init__(self) -> None:
        self._thread_id: str | None = None
        self._thread_started_handler = None
        self.reset_calls = 0
        self.persist_calls: list[dict] = []
        self.closed = False

    def set_thread_started_handler(self, handler) -> None:
        self._thread_started_handler = handler

    def start_thread(self, thread_id: str) -> None:
        self._thread_id = thread_id
        assert self._thread_started_handler is not None
        self._thread_started_handler(self, thread_id)

    def reset_voice_route_to_dispatcher(self) -> None:
        self.reset_calls += 1

    def persist_voice_route(self, **kwargs) -> None:
        self.persist_calls.append(kwargs)

    async def aclose(self) -> None:
        self.closed = True


def _home(tmp_path: Path) -> DirectVoiceHome:
    return DirectVoiceHome(
        label="Voice call Sep 29 2:14 PM",
        voice_id="voice-123",
        voice_name="Theo",
        route_state_path=tmp_path / "livekit-voice-route.json",
        cwd="/tmp/project",
    )


def _route_file(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "livekit-voice-route.json").read_text())


def test_voice_mode_config_round_trip(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    monkeypatch.delenv("OPENBASE_VOICE_MODE", raising=False)
    assert dispatcher_config.voice_mode(config_path) == "dispatcher"

    dispatcher_config.set_voice_mode("direct", config_path)
    assert dispatcher_config.voice_mode(config_path) == "direct"

    with pytest.raises(ValueError):
        dispatcher_config.set_voice_mode("nope", config_path)

    config_path.write_text(json.dumps({"voice_mode": "bogus"}))
    monkeypatch.setenv("OPENBASE_VOICE_MODE", "direct")
    assert dispatcher_config.voice_mode(config_path) == "direct"


def test_direct_voice_mode_instructions_omit_exit_to_dispatch(tmp_path: Path) -> None:
    assert "exit-to-dispatch" in DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS
    assert "exit-to-dispatch" not in DIRECT_VOICE_MODE_BUILTIN_DEVELOPER_INSTRUCTIONS
    assert "dispatcher" not in DIRECT_VOICE_MODE_BUILTIN_DEVELOPER_INSTRUCTIONS.lower()

    loaded = livekit_config.load_direct_voice_mode_developer_instructions(
        env={}, default_path=tmp_path / "missing.md"
    )
    assert loaded == DIRECT_VOICE_MODE_BUILTIN_DEVELOPER_INSTRUCTIONS

    # An installed instruction file still wins over the builtin.
    custom = tmp_path / "VOICE_INSTRUCTIONS.md"
    custom.write_text("- Keep it short.\n")
    assert (
        livekit_config.load_direct_voice_mode_developer_instructions(
            env={}, default_path=custom
        )
        == "- Keep it short."
    )


def test_direct_home_router_has_no_dispatcher(tmp_path: Path) -> None:
    # Seed the route file with a previous call's target; a new direct call
    # must clear it but keep the dispatcher fields alone.
    (tmp_path / "livekit-voice-route.json").write_text(
        json.dumps(
            {
                "dispatcher_thread_id": "disp-1",
                "dispatcher_voice_id": "v-disp",
                "active_target_thread_id": "old-target",
                "active_target_kind": "codex_thread",
            }
        )
    )
    home_client = _FakeHomeClient()
    router = LiveKitVoiceRouter(home_client, direct_home=_home(tmp_path))

    assert router.is_direct_mode
    assert router.is_home_active
    assert not router.is_dispatcher_active
    assert router.active_target_voice_id == "voice-123"
    assert router.active_target_voice_name == "Theo"
    assert router.route_snapshot().active_route == "direct"
    assert router.home_route_label == "your call thread"
    assert router.exit_to_dispatch() is False

    state = _route_file(tmp_path)
    assert state["dispatcher_thread_id"] == "disp-1"
    assert state["dispatcher_voice_id"] == "v-disp"
    assert state["active_target_thread_id"] is None

    # The thread is created lazily; once it exists it becomes the active target.
    home_client.start_thread("thread-direct-1")
    state = _route_file(tmp_path)
    assert state["active_target_thread_id"] == "thread-direct-1"
    assert state["active_target_kind"] == "direct"
    assert state["active_target_label"] == "Voice call Sep 29 2:14 PM"
    assert state["active_target_voice_name"] == "Theo"
    assert state["dispatcher_thread_id"] == "disp-1"
    assert router.route_snapshot().active_thread_id == "thread-direct-1"
    assert home_client.reset_calls == 0
    assert home_client.persist_calls == []


def test_dispatcher_router_unchanged_without_direct_home(tmp_path: Path) -> None:
    home_client = _FakeHomeClient()
    router = LiveKitVoiceRouter(home_client)

    assert not router.is_direct_mode
    assert router.is_home_active
    assert router.is_dispatcher_active
    assert router.active_target_voice_id is None
    assert router.route_snapshot().active_route == "dispatcher"
    assert router.home_route_label == "dispatch"
    assert not (tmp_path / "livekit-voice-route.json").exists()
