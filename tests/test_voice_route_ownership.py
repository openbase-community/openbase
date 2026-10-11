import asyncio
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


@pytest.mark.parametrize("already_dispatcher", [True, False])
@pytest.mark.parametrize("prepare_fails", [True, False])
async def test_return_cancels_pending_transfer_without_late_route_theft(
    monkeypatch, tmp_path, already_dispatcher, prepare_fails
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    started, finish = asyncio.Event(), asyncio.Event()

    class PendingTarget(TargetClient):
        async def prepare(self):
            if self._thread_id == "pending":
                started.set()
                await finish.wait()
                if prepare_fails:
                    raise RuntimeError("obsolete preparation failed")

    monkeypatch.setattr(voice_routing, "SuperAgentsLiveKitClient", PendingTarget)
    path = tmp_path / "livekit-voice-route.json"
    router = voice_routing.LiveKitVoiceRouter(DispatcherClient(path))
    if not already_dispatcher:
        await router.transfer_to_thread(thread_id="current", cwd=".", label="Current")
    pending = asyncio.create_task(
        router.transfer_to_thread(thread_id="pending", cwd=".", label="Pending")
    )
    await started.wait()
    assert router.has_pending_transfer
    router.exit_to_dispatch()
    expected = router.route_snapshot()
    finish.set()
    assert await pending is False
    assert router.is_dispatcher_active
    assert router.route_snapshot() == expected
    assert not router.has_pending_transfer
    assert (
        livekit_voice_route.get_livekit_voice_route_state().active_target_thread_id
        is None
    )
    await router.close()


async def test_newer_transfer_wins_when_old_preparation_finishes_last(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    started, finish = asyncio.Event(), asyncio.Event()

    class PendingTarget(TargetClient):
        async def prepare(self):
            if self._thread_id == "older":
                started.set()
                await finish.wait()

    monkeypatch.setattr(voice_routing, "SuperAgentsLiveKitClient", PendingTarget)
    router = voice_routing.LiveKitVoiceRouter(
        DispatcherClient(tmp_path / "livekit-voice-route.json")
    )
    older = asyncio.create_task(
        router.transfer_to_thread(thread_id="older", cwd=".", label="Older")
    )
    await started.wait()
    assert await router.transfer_to_thread(thread_id="newer", cwd=".", label="Newer")
    finish.set()
    assert await older is False
    assert router.route_snapshot().active_thread_id == "newer"
    assert (
        livekit_voice_route.get_livekit_voice_route_state().active_target_thread_id
        == "newer"
    )
    await router.close()


@pytest.mark.parametrize("live", [True, False])
async def test_superseded_transfer_does_not_announce_or_replace_character(live):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from openbase_coder_cli.livekit_agent.livekit import _transfer_live_voice_route
    from openbase_coder_cli.livekit_agent.packets import VoiceRouteCommand

    router = SimpleNamespace(transfer_to_thread=AsyncMock(return_value=False))
    sink = Mock()
    command = VoiceRouteCommand(action="transfer_to_thread", thread_id="old", cwd=".")
    if live:
        await _transfer_live_voice_route(router, command, sink)
        sink.notify_route_changed.assert_not_called()
        sink.announce.assert_not_called()
    else:
        await voice_routing._transfer_voice_route(router, command, sink)
        sink.enqueue.assert_not_called()


async def test_live_transfer_passes_the_announce_flag_to_the_character_owner():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from openbase_coder_cli.livekit_agent.livekit import _transfer_live_voice_route
    from openbase_coder_cli.livekit_agent.packets import VoiceRouteCommand

    router = SimpleNamespace(transfer_to_thread=AsyncMock(return_value=True))
    sink = Mock()
    command = VoiceRouteCommand(
        action="transfer_to_thread", thread_id="t", cwd=".", label="Cooper", announce=False
    )
    await _transfer_live_voice_route(router, command, sink)
    sink.notify_route_changed.assert_called_once_with(
        action="transfer_to_thread", agent_label="Cooper", announce=False
    )


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


async def test_agent_requested_transfer_is_not_announced_twice():
    """2026-10-10: Cooper said "Connected to Cooper." and the announcer then
    added "Voice route transferred." The agent's own words are enough; a
    transfer from a thread menu still gets one natural confirmation."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from openbase_coder_cli.livekit_agent.packets import (
        VoiceRouteCommand,
        voice_route_command_from_payload,
    )

    router = SimpleNamespace(
        transfer_to_thread=AsyncMock(return_value=True),
        active_target_voice_id="voice-cooper",
        active_target_voice_name="Cooper",
    )
    quiet = voice_route_command_from_payload(
        {
            "action": "transfer_to_thread",
            "thread_id": "t",
            "cwd": ".",
            "announce": False,
        }
    )
    assert quiet is not None and quiet.announce is False
    sink = Mock()
    await voice_routing._transfer_voice_route(router, quiet, sink)
    sink.enqueue.assert_not_called()

    spoken = VoiceRouteCommand(action="transfer_to_thread", thread_id="t", cwd=".")
    assert spoken.announce
    await voice_routing._transfer_voice_route(router, spoken, sink)
    message = sink.enqueue.call_args.args[0]
    assert message.text == "You're now talking with Cooper."
    assert message.voice_id == "voice-cooper"


async def test_transfer_and_return_leave_events_in_both_transcripts(
    monkeypatch, tmp_path
):
    from openbase_coder_cli.thread_events import list_thread_events

    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(voice_routing, "SuperAgentsLiveKitClient", TargetClient)
    path = tmp_path / "livekit-voice-route.json"
    dispatcher = DispatcherClient(path)
    router = voice_routing.LiveKitVoiceRouter(dispatcher)
    dispatcher_id = getattr(dispatcher, "_thread_id", "") or ""
    await router.transfer_to_thread(
        thread_id="s_cooper", cwd=str(tmp_path), label="Cooper", voice_name="Cooper"
    )
    assert router.exit_to_dispatch() is True
    assert [e["text"] for e in list_thread_events("s_cooper")] == [
        "Call transferred here from the Dispatcher.",
        "Call returned to the Dispatcher.",
    ]
    if dispatcher_id:
        assert [e["text"] for e in list_thread_events(dispatcher_id)] == [
            "Call transferred to Cooper.",
            "Back with the Dispatcher, from Cooper.",
        ]


def test_a_new_call_resumes_the_dispatcher_thread_again():
    """The shared Dispatcher client must not keep instructions another writer
    left on the backend session (Maritime, 2026-10-10 22:16Z refusal)."""
    from unittest.mock import Mock

    client = Mock()
    voice_routing.LiveKitVoiceRouter(client)
    client.reload_thread_on_next_use.assert_called_once()
    # Fakes without the hook (older clients) still construct.
    voice_routing.LiveKitVoiceRouter(object())
