from pathlib import Path

import pytest

from openbase_coder_cli import livekit_voice_route
from openbase_coder_cli.livekit_agent import voice_routing
from openbase_coder_cli.livekit_agent.super_agents_client_threads import (
    SuperAgentsClientThreadsMixin,
)


class DispatcherClient(SuperAgentsClientThreadsMixin):
    def __init__(self, path: Path):
        self._state_path = path
        self._thread_id = "dispatcher-thread"
        self._backend_client = None

    def _dispatcher_voice(self):
        return {"id": "dispatcher-voice", "name": "Dispatcher"}


class TargetClient:
    def __init__(self, **kwargs):
        self._thread_id = kwargs["initial_thread_id"]

    async def prepare(self):
        return self._thread_id

    async def aclose(self):
        pass


@pytest.mark.parametrize("next_thread", ["first-thread", "second-thread"])
async def test_old_call_shutdown_does_not_clear_new_call_route(
    monkeypatch, tmp_path, next_thread
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(voice_routing, "SuperAgentsLiveKitClient", TargetClient)
    path = tmp_path / "livekit-voice-route.json"
    old_call = voice_routing.LiveKitVoiceRouter(DispatcherClient(path))
    new_call = voice_routing.LiveKitVoiceRouter(DispatcherClient(path))
    await old_call.transfer_to_thread(
        thread_id="first-thread", cwd=str(tmp_path), label="First"
    )
    await new_call.transfer_to_thread(
        thread_id=next_thread, cwd=str(tmp_path), label="Next"
    )
    active = path.read_text()

    await old_call.close()

    assert path.read_text() == active
    assert (
        livekit_voice_route.get_livekit_voice_route_state().active_target_thread_id
        == next_thread
    )
    await new_call.close()
    assert (
        livekit_voice_route.get_livekit_voice_route_state().active_target_thread_id
        is None
    )


async def test_route_publication_preserves_shutdown_ownership(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(voice_routing, "SuperAgentsLiveKitClient", TargetClient)
    router = voice_routing.LiveKitVoiceRouter(
        DispatcherClient(tmp_path / "livekit-voice-route.json")
    )
    await router.transfer_to_thread(
        thread_id="target-thread", cwd=str(tmp_path), label="Target"
    )
    livekit_voice_route._write_state(
        livekit_voice_route.get_livekit_voice_route_state()
    )

    await router.close()

    assert (
        livekit_voice_route.get_livekit_voice_route_state().active_target_thread_id
        is None
    )


async def test_old_call_shutdown_does_not_restore_a_recreated_dispatcher(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(voice_routing, "SuperAgentsLiveKitClient", TargetClient)
    path = tmp_path / "livekit-voice-route.json"
    router = voice_routing.LiveKitVoiceRouter(DispatcherClient(path))
    await router.transfer_to_thread(
        thread_id="target-thread", cwd=str(tmp_path), label="Target"
    )
    livekit_voice_route.prepare_livekit_dispatcher_recreation()
    reset_state = path.read_text()

    await router.close()

    assert path.read_text() == reset_state
    assert (
        livekit_voice_route.get_livekit_voice_route_state().dispatcher_thread_id is None
    )
