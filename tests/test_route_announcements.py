"""GPT-Live transfer and return announcements: Classic's words, voices and moments."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from livekit import rtc
from livekit.agents import llm

from openbase_coder_cli.livekit_agent import live_characters as module
from openbase_coder_cli.livekit_agent import route_announcements
from openbase_coder_cli.livekit_agent.live_characters import LiveCharacterController
from openbase_coder_cli.livekit_agent.route_announcements import (
    BACK_TO_DISPATCH_TEXT,
    TRANSFERRED_TEXT,
    RouteAnnouncer,
)


def _announcer():
    tts = Mock()
    tts.resolve_voice_id.side_effect = lambda voice: voice or "announcer-default"
    return RouteAnnouncer(tts=tts), tts


def test_every_transfer_and_return_is_announced_in_the_neutral_voice():
    # Gabe, 2026-10-10: "Voice route transferred" on every transfer, spoken
    # ones too, in one announcer voice rather than the agent's Cartesia voice.
    announcer, _ = _announcer()
    back = announcer.message_for("exit_to_dispatch")
    assert (back.text, back.voice_id) == (BACK_TO_DISPATCH_TEXT, None)
    for announce in (True, False):
        moved = announcer.message_for(
            "transfer_to_thread", announce=announce, agent_label="Cooper"
        )
        assert (moved.text, moved.voice_id) == (TRANSFERRED_TEXT, None)
    assert TRANSFERRED_TEXT == "Voice route transferred."
    assert announcer.message_for(None) is None


async def test_announce_synthesizes_with_the_announcer_tts_and_awaits_playout(
    monkeypatch,
):
    announcer, tts = _announcer()
    seen = {}

    async def fake_audio(given_tts, text, *, voice_id, outcome):
        seen.update(tts=given_tts, text=text, voice_id=voice_id)
        outcome.audio_events = 3
        outcome.audio_seconds = 1.1
        outcome.completed = True
        yield rtc.AudioFrame(
            data=b"\0\0" * 480,
            sample_rate=24000,
            num_channels=1,
            samples_per_channel=480,
        )

    monkeypatch.setattr(route_announcements, "announcement_audio", fake_audio)
    captured = {}

    async def playout():
        # The SDK drains the supplied audio while it plays it out.
        async for _ in captured["audio"]:
            pass

    handle = SimpleNamespace(
        wait_for_playout=AsyncMock(side_effect=playout), interrupted=False
    )
    session = Mock()
    session.say.side_effect = lambda text, **kwargs: (
        captured.update(kwargs, text=text) or handle
    )

    assert await announcer.announce(session, "exit_to_dispatch") is True
    assert captured["text"] == BACK_TO_DISPATCH_TEXT
    # GPT-Live rejects allow_interruptions=False; the speech gate handles echo.
    assert "allow_interruptions" not in captured
    assert captured["add_to_chat_ctx"] is False
    assert seen == {
        "tts": tts,
        "text": BACK_TO_DISPATCH_TEXT,
        "voice_id": None,
    }
    handle.wait_for_playout.assert_awaited_once()
    # Something that is not a route move does not touch the session.
    assert await announcer.announce(session, None) is False
    assert session.say.call_count == 1


def _controller(monkeypatch, current, labels, announce_route):
    router = Mock()
    router.route_snapshot.side_effect = lambda: SimpleNamespace(
        active_thread_id=current["id"]
    )
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
        announce_route=announce_route,
    )
    controller._replace = AsyncMock(return_value=SimpleNamespace(duplex_session=Mock()))
    return controller, bridge


async def test_handoff_announces_before_the_greeting_and_the_return_after_it(
    monkeypatch,
):
    # 2026-10-10: a GPT-Live transfer or return was silent; Classic speaks both.
    order = []

    async def announce_route(session, action, *, announce, agent_label):
        order.append((action, announce, agent_label))
        return True

    current = {"id": "dispatcher"}
    controller, bridge = _controller(
        monkeypatch, current, {"dispatcher": None, "cooper": "Cooper"}, announce_route
    )
    bridge.greet.side_effect = lambda text: order.append(f"greet:{text}")
    history = llm.ChatContext()

    current["id"] = "cooper"
    controller.route_changed("transfer_to_thread", announce=True, agent_label="Cooper")
    await controller._conversation(history)
    assert order == [("transfer_to_thread", True, "Cooper"), "greet:Hi, I'm Cooper."]

    current["id"] = "dispatcher"
    controller.route_changed("exit_to_dispatch")
    await controller._conversation(history)
    assert order[2:] == [("exit_to_dispatch", True, None)]

    # A restore after a temporary announcement character moves no route: silent.
    await controller._conversation(history)
    assert order[3:] == []

    # The dispatcher that transfers in its own words: the announcer decides (announce=False).
    current["id"] = "cooper"
    controller.route_changed("transfer_to_thread", announce=False, agent_label="Cooper")
    await controller._conversation(history)
    assert order[3:] == [("transfer_to_thread", False, "Cooper")]


async def test_a_failed_announcement_keeps_the_call_and_the_greeting(monkeypatch):
    current = {"id": "dispatcher"}
    controller, bridge = _controller(
        monkeypatch,
        current,
        {"dispatcher": None, "lucy": "Lucy"},
        AsyncMock(side_effect=RuntimeError("tts down")),
    )
    current["id"] = "lucy"
    controller.route_changed("transfer_to_thread", agent_label="Lucy")
    await controller._conversation(llm.ChatContext())
    bridge.greet.assert_called_once_with("Hi, I'm Lucy.")
    assert controller._route_announcement is None


async def test_without_an_announcer_route_moves_stay_silent(monkeypatch):
    current = {"id": "dispatcher"}
    controller, bridge = _controller(
        monkeypatch, current, {"dispatcher": None, "lucy": "Lucy"}, None
    )
    current["id"] = "lucy"
    controller.route_changed("transfer_to_thread", agent_label="Lucy")
    await controller._conversation(llm.ChatContext())
    bridge.greet.assert_called_once_with("Hi, I'm Lucy.")
