"""GPT-Live transfer and return clips: when they play, and that they decode."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from livekit.agents import llm

from openbase_coder_cli.livekit_agent import live_characters as module
from openbase_coder_cli.livekit_agent.live_characters import LiveCharacterController
from openbase_coder_cli.livekit_agent.route_announcements import (
    BACK_TO_DISPATCH_SOUND,
    ROUTE_TRANSFERRED_SOUND,
    play_route_announcement,
    route_announcement_path,
)
from openbase_coder_cli.livekit_agent.speech_queue import _decode_audio_file


def test_return_always_announces_and_a_transfer_only_when_asked():
    # Mirrors Classic: ``openbase-coder user transfer-to-agent`` sends
    # announce=False because the dispatcher confirms the move in its own words.
    assert route_announcement_path("exit_to_dispatch").name == BACK_TO_DISPATCH_SOUND
    assert route_announcement_path("exit_to_dispatch", announce=False).name == BACK_TO_DISPATCH_SOUND
    assert route_announcement_path("transfer_to_thread").name == ROUTE_TRANSFERRED_SOUND
    assert route_announcement_path("transfer_to_thread", announce=False) is None
    assert route_announcement_path(None) is None


def test_bundled_clips_decode_to_short_mono_audio():
    for action in ("exit_to_dispatch", "transfer_to_thread"):
        path = route_announcement_path(action)
        assert path.is_file(), path
        frames = _decode_audio_file(path, sample_rate=24000)
        seconds = sum(frame.samples_per_channel / frame.sample_rate for frame in frames)
        assert 0.5 < seconds < 3.0, (path.name, seconds)
        assert all(frame.num_channels == 1 and frame.sample_rate == 24000 for frame in frames)


async def test_play_route_announcement_uses_the_session_output_without_history():
    handle = SimpleNamespace(wait_for_playout=AsyncMock(), interrupted=False)
    session = Mock()
    session.say.return_value = handle
    path = route_announcement_path("exit_to_dispatch")
    assert await play_route_announcement(session, path) is True
    kwargs = session.say.call_args.kwargs
    assert session.say.call_args.args == ("",)
    assert kwargs["allow_interruptions"] is False
    assert kwargs["add_to_chat_ctx"] is False
    handle.wait_for_playout.assert_awaited_once()


def _controller(monkeypatch, current, labels):
    router = Mock()
    router.route_snapshot.side_effect = lambda: SimpleNamespace(active_thread_id=current["id"])
    router.can_deliver_for_snapshot.return_value = True
    bridge = Mock()
    bridge.starting_agent_label.side_effect = lambda: labels[current["id"]]
    monkeypatch.setattr(module, "route_voice_identity", lambda _: Mock())
    controller = LiveCharacterController(
        session=Mock(),
        bridge=bridge,
        router=router,
        model_factory=Mock(),
        instructions=Mock(),
        on_error=AsyncMock(),
    )
    controller._replace = AsyncMock(return_value=SimpleNamespace(duplex_session=Mock()))
    return controller, bridge


async def test_handoff_plays_the_clip_before_the_greeting_then_the_return_clip(monkeypatch):
    # 2026-10-10: a GPT-Live transfer or return was silent; Classic chimes both.
    current = {"id": "dispatcher"}
    controller, bridge = _controller(
        monkeypatch, current, {"dispatcher": None, "cooper": "Cooper"}
    )
    order = []
    monkeypatch.setattr(
        module,
        "play_route_announcement",
        AsyncMock(side_effect=lambda session, path: order.append(path.name) or True),
    )
    bridge.greet.side_effect = lambda text: order.append(f"greet:{text}")
    history = llm.ChatContext()

    current["id"] = "cooper"
    controller.route_changed("transfer_to_thread", announce=True)
    await controller._conversation(history)
    assert order == [ROUTE_TRANSFERRED_SOUND, "greet:Hi, I'm Cooper."]

    current["id"] = "dispatcher"
    controller.route_changed("exit_to_dispatch")
    await controller._conversation(history)
    assert order[2:] == [BACK_TO_DISPATCH_SOUND]

    # A restore after a temporary announcement character moves no route: silent.
    await controller._conversation(history)
    assert order[3:] == []

    # The dispatcher that transfers in its own words gets no generic chime.
    current["id"] = "cooper"
    controller.route_changed("transfer_to_thread", announce=False)
    await controller._conversation(history)
    assert order[3:] == []


async def test_a_failed_clip_keeps_the_call_and_the_greeting(monkeypatch):
    current = {"id": "dispatcher"}
    controller, bridge = _controller(monkeypatch, current, {"dispatcher": None, "lucy": "Lucy"})
    monkeypatch.setattr(
        module, "play_route_announcement", AsyncMock(side_effect=RuntimeError("no output"))
    )
    current["id"] = "lucy"
    controller.route_changed("transfer_to_thread")
    await controller._conversation(llm.ChatContext())
    bridge.greet.assert_called_once_with("Hi, I'm Lucy.")
    assert controller._route_announcement is None
