"""Character handoff against the installed SDK and a loopback GPT-Live gateway."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from livekit.agents import AgentSession, llm
from livekit.plugins.openai.realtime import GPTLiveModel
from test_live_voice import FakeGPTLiveServer

from openbase_coder_cli.livekit_agent.live_characters import (
    CharacterAssistant,
    LiveCharacterController,
    bounded_history,
)
from openbase_coder_cli.livekit_agent.live_preconnect import wait_live_session_started
from openbase_coder_cli.livekit_agent.packets import AnnouncerMessage
from openbase_coder_cli.voice_identity import agent_voice_identity


def test_agent_mapping_ignores_dispatcher_override(monkeypatch):
    from openbase_coder_cli.cartesia_voice_catalog import CARTESIA_VOICE_CATALOG

    monkeypatch.setenv("LIVEKIT_LIVE_VOICE_VOICE", "marin")
    entries = list(CARTESIA_VOICE_CATALOG)
    for entry in entries:
        identity = agent_voice_identity(entry.id, provider_id="cartesia")
        assert identity.gpt_live_voice == entry.gpt_live_voice


def test_bounded_history_keeps_recent_text_and_excludes_old_personas():
    history = llm.ChatContext()
    history.add_message(role="system", content="Old character")
    for i in range(100):
        history.add_message(role="user", content=f"request {i}")
    bounded = bounded_history(history)
    assert len(bounded.items) == 64
    assert bounded.items[-1].text_content == "request 99"
    assert all(item.role == "user" for item in bounded.items)


async def test_real_sdk_handoff_changes_immutable_voice_and_preserves_history():
    async with FakeGPTLiveServer() as server:
        models = []

        def model(voice):
            value = GPTLiveModel(
                voice=voice,
                delegation="client",
                api_key="cloud-token",
                base_url=server.base_url,
            )
            models.append(value)
            return value

        history = llm.ChatContext()
        history.add_message(role="user", content="Remember the blue counter.")
        first = CharacterAssistant(
            model=model("marin"), instructions="Dispatcher", history=history
        )
        session = AgentSession()
        try:
            await session.start(agent=first)
            await wait_live_session_started(first.duplex_session, timeout=5)
            assert (
                server.session_start["session"]["audio"]["output"]["voice"] == "marin"
            )
            old = first.duplex_session
            controller = LiveCharacterController(
                session=session,
                bridge=Mock(),
                router=Mock(),
                model_factory=model,
                instructions=lambda label: label,
                on_error=AsyncMock(),
                timeout=5,
            )
            identity = SimpleNamespace(gpt_live_voice="cedar", voice_id="agent-voice")
            second = await controller._replace(
                identity=identity,
                history=bounded_history(first.chat_ctx),
                instructions="Oliver",
            )
            assert old._closing
            assert second.duplex_session is not old
            assert (
                server.session_start["session"]["audio"]["output"]["voice"] == "cedar"
            )
            assert "blue counter" in str(server.session_start["session"]["input"])
            assert server.session_start["session"]["instructions"] == "Oliver"
        finally:
            await session.aclose()
            for value in models:
                await value.aclose()


async def test_announcement_restores_route_and_holds_backend_speech(monkeypatch):
    import openbase_coder_cli.livekit_agent.live_characters as module

    session = SimpleNamespace(
        current_agent=SimpleNamespace(chat_ctx=llm.ChatContext()),
        agent_state="listening",
    )
    session.current_agent.chat_ctx.add_message(role="user", content="Continue my task")
    bridge = Mock()
    router = Mock()
    controller = LiveCharacterController(
        session=session,
        bridge=bridge,
        router=router,
        model_factory=Mock(),
        instructions=Mock(),
        on_error=AsyncMock(),
    )
    monkeypatch.setattr(
        module,
        "agent_voice_identity",
        lambda _: SimpleNamespace(gpt_live_voice="cedar", voice_name="Oliver"),
    )
    live = Mock()

    async def replace(**kwargs):
        assert controller.announcing
        assert kwargs["history"].items == []
        return SimpleNamespace(duplex_session=live)

    controller._replace = replace
    controller._conversation = AsyncMock()

    def speak(*args, **kwargs):
        controller._announcement_stop.set()

    live.append_commentary.side_effect = speak
    await controller._announcement(
        AnnouncerMessage("id", "Hi, I'm Oliver.", "oliver", "Oliver")
    )
    bridge.suspend_session.assert_called_once()
    router.transfer_to_thread.assert_not_called()
    router.exit_to_dispatch.assert_not_called()
    controller._conversation.assert_awaited_once()
    restored = controller._conversation.call_args.args[0]
    assert restored.items[0].text_content == "Continue my task"
    assert not controller.announcing


async def test_queue_deduplicates_and_shutdown_cancels_owned_worker():
    controller = LiveCharacterController(
        session=Mock(),
        bridge=Mock(),
        router=Mock(),
        model_factory=Mock(),
        instructions=Mock(),
        on_error=AsyncMock(),
    )
    message = AnnouncerMessage("same", "Done", "voice", "Agent")
    controller.announce(message)
    controller.announce(message)
    assert controller._queue.qsize() == 1
    controller.session.aclose = AsyncMock()
    controller.start()
    await controller.close()
    assert controller._task.done()


async def test_route_race_during_handoff_attaches_only_latest_character(monkeypatch):
    import openbase_coder_cli.livekit_agent.live_characters as module

    bridge = Mock()
    router = Mock()
    router.can_deliver_for_snapshot.side_effect = [False, True]
    monkeypatch.setattr(
        module,
        "route_voice_identity",
        lambda _: SimpleNamespace(gpt_live_voice="cedar"),
    )
    controller = LiveCharacterController(
        session=Mock(),
        bridge=bridge,
        router=router,
        model_factory=Mock(),
        instructions=Mock(),
        on_error=AsyncMock(),
    )
    first, second = Mock(), Mock()
    controller._replace = AsyncMock(side_effect=[first, second])
    await controller._conversation(llm.ChatContext())
    assert controller._replace.await_count == 2
    bridge.attach.assert_called_once_with(second.duplex_session)
    bridge.on_session_reconnected.assert_called_once()


async def test_transfer_or_talkover_wakes_and_cancels_announcement():
    bridge = Mock()
    controller = LiveCharacterController(
        session=Mock(),
        bridge=bridge,
        router=Mock(),
        model_factory=Mock(),
        instructions=Mock(),
        on_error=AsyncMock(),
    )
    controller._announcing = True
    controller.route_changed()
    bridge.suspend_session.assert_called_once()
    assert controller._route_pending
    assert controller._announcement_stop.is_set()
    assert controller._speech_changed.is_set()
    controller._announcement_stop.clear()
    controller.user_state_changed(SimpleNamespace(new_state="speaking"))
    assert controller._announcement_stop.is_set()
