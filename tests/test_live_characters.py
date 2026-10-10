"""Character handoff against the installed SDK and a loopback GPT-Live gateway."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import AgentSession, llm
from livekit.plugins.openai.realtime import GPTLiveModel
from test_live_voice import FakeGPTLiveServer, _close_caller_utterance

from openbase_coder_cli.livekit_agent.config import live_voice_startup_instructions
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
            model=model("marin"),
            instructions=live_voice_startup_instructions(),
            history=history,
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
                instructions=live_voice_startup_instructions(agent_label="Blake"),
            )
            assert old._closing
            assert second.duplex_session is not old
            assert (
                server.session_start["session"]["audio"]["output"]["voice"] == "cedar"
            )
            assert "blue counter" in str(server.session_start["session"]["input"])
            assert (
                "Your name in this call is Blake."
                in server.session_start["session"]["instructions"]
            )
            assert (
                "Your name in this call is Dispatcher."
                not in server.session_start["session"]["instructions"]
            )
        finally:
            await session.aclose()
            for value in models:
                await value.aclose()


async def test_announcement_restores_route_and_holds_backend_speech(monkeypatch):
    import openbase_coder_cli.livekit_agent.live_characters as module

    session = SimpleNamespace(
        current_agent=SimpleNamespace(chat_ctx=llm.ChatContext()),
        agent_state="listening",
        user_state="listening",
        output=Mock(audio_enabled=True),
        interrupt=Mock(side_effect=lambda **_: asyncio.sleep(0)),
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
        assert "Your name in this call is Oliver." in kwargs["instructions"]
        kwargs["on_enter"](live)
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
    bridge.on_character_session_started.assert_called_once()
    bridge.on_session_reconnected.assert_not_called()


async def test_each_route_introduces_once_across_returns_and_announcement_restore(
    monkeypatch,
):
    import openbase_coder_cli.livekit_agent.live_characters as module

    current = {"id": "dispatcher"}
    labels = {"dispatcher": None, "blake": "Blake", "lucy": "Lucy"}
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
    )
    controller._replace = AsyncMock(return_value=SimpleNamespace(duplex_session=Mock()))
    history = llm.ChatContext()
    history.add_message(role="user", content="Keep the blue counter.")
    # First contact, duplicate route event, temporary announcement restore,
    # return to Dispatcher, return to Blake, first contact with another agent.
    for route in ["blake", "blake", "blake", "dispatcher", "blake", "lucy"]:
        current["id"] = route
        await controller._conversation(history)
    assert [call.args[0] for call in bridge.announce.call_args_list] == [
        "Hi, I'm Blake.",
        "Hi, I'm Lucy.",
    ]
    assert controller._replace.await_count == 6
    assert all(
        call.kwargs["history"] is history for call in controller._replace.call_args_list
    )
    bridge.on_session_reconnected.assert_not_called()


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


@pytest.mark.parametrize("ending", ["complete", "transfer", "timeout"])
async def test_real_announcement_talkover_preserves_final_once_and_rejects_old_route(
    monkeypatch, ending
):
    from test_live_delegation import FakeVoiceClient, _make_bridge, _settle

    import openbase_coder_cli.livekit_agent.live_characters as module

    bridge, original, router, dispatcher, ledger, lifecycle = _make_bridge()
    identity = SimpleNamespace(
        gpt_live_voice="cedar", voice_id="voice", voice_name="Oliver"
    )
    monkeypatch.setattr(module, "agent_voice_identity", lambda _: identity)
    monkeypatch.setattr(module, "route_voice_identity", lambda _: identity)
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

        first = CharacterAssistant(
            model=model("marin"), instructions="Dispatcher", history=llm.ChatContext()
        )
        session = AgentSession()
        controller = LiveCharacterController(
            session=session,
            bridge=bridge,
            router=router,
            model_factory=model,
            instructions=lambda label: "Conversation",
            on_error=AsyncMock(),
            initial_model=models[0],
            caller_drain_timeout=0.1 if ending == "timeout" else 12.0,
        )
        session.on("user_state_changed", controller.user_state_changed)
        bridge.character_route_changed = controller.route_changed
        try:
            await session.start(agent=first)
            await wait_live_session_started(first.duplex_session, timeout=5)
            bridge.attach(first.duplex_session)
            controller._task = task = asyncio.create_task(
                controller._announcement(
                    AnnouncerMessage("id", "The work is ready.", "voice", "Oliver")
                )
            )
            await server.wait_for_append("commentary")
            announcement = session.current_agent.duplex_session
            await server.send(
                {
                    "type": "session.input_transcript.delta",
                    "delta": "Please check",
                    "start_ms": 0,
                    "end_ms": 300,
                }
            )
            async with asyncio.timeout(5):
                while not controller._announcement_stop.is_set():
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.02)
            assert not task.done()
            assert not announcement._closing
            assert not session.output.audio_enabled
            assert dispatcher.prompts == []
            if ending == "transfer":
                other = FakeVoiceClient(thread_id="other")
                router.transfer(other)
                controller.route_changed()
                announcement._end_speech("user")
            elif ending == "complete":
                await server.send(
                    {
                        "type": "session.input_transcript.delta",
                        "delta": " the entire change.",
                        "start_ms": 300,
                        "end_ms": 700,
                    }
                )
                await asyncio.sleep(0.02)
                await _close_caller_utterance(announcement)
            await asyncio.wait_for(task, 5)
            await _settle()
            texts = [
                item.text_content
                for item in session.current_agent.chat_ctx.items
                if isinstance(item, llm.ChatMessage) and item.role == "user"
            ]
            if ending == "transfer":
                assert dispatcher.prompts == []
                assert other.prompts == []
                assert texts == []
            else:
                assert len(dispatcher.prompts) == 1
                expected = (
                    "Please check the entire change."
                    if ending == "complete"
                    else "Please check"
                )
                assert expected in dispatcher.prompts[0][0]
                assert texts == [expected]
            assert session.output.audio_enabled
        finally:
            await controller.close()
            await bridge.aclose()
            for value in models:
                await value.aclose()


async def test_failed_handoff_closes_both_owned_models():
    previous, replacement = Mock(), Mock()
    previous.aclose = AsyncMock()
    replacement.aclose = AsyncMock()
    session = Mock(interrupt=AsyncMock(), aclose=AsyncMock())
    controller = LiveCharacterController(
        session=session,
        bridge=Mock(),
        router=Mock(),
        model_factory=lambda _: replacement,
        instructions=Mock(),
        on_error=AsyncMock(),
        initial_model=previous,
        timeout=0.01,
    )
    with pytest.raises(TimeoutError):
        await controller._replace(
            identity=SimpleNamespace(gpt_live_voice="cedar"),
            history=llm.ChatContext(),
            instructions="Agent",
        )
    previous.aclose.assert_awaited_once()
    await controller.close()
    replacement.aclose.assert_awaited_once()


async def test_transfer_during_announcement_start_never_injects_old_commentary(
    monkeypatch,
):
    import openbase_coder_cli.livekit_agent.live_characters as module

    identity = SimpleNamespace(
        gpt_live_voice="cedar", voice_id="voice", voice_name="Oliver"
    )
    monkeypatch.setattr(module, "agent_voice_identity", lambda _: identity)
    session = SimpleNamespace(
        current_agent=SimpleNamespace(chat_ctx=llm.ChatContext()),
        agent_state="listening",
        user_state="listening",
        output=Mock(audio_enabled=True),
    )

    def interrupt(**kwargs):
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    session.interrupt = interrupt
    controller = LiveCharacterController(
        session=session,
        bridge=Mock(),
        router=Mock(),
        model_factory=Mock(),
        instructions=Mock(),
        on_error=AsyncMock(),
    )
    live = Mock()

    async def replace(**kwargs):
        kwargs["on_enter"](live)
        controller.route_changed()
        return SimpleNamespace(duplex_session=live)

    controller._replace = replace
    controller._conversation = AsyncMock()
    await controller._announcement(
        AnnouncerMessage("id", "Old route notice", "voice", "Oliver")
    )
    live.append_commentary.assert_not_called()
    live.off.assert_called_once()
    controller._conversation.assert_awaited_once()


def test_bounded_history_drops_nontext_payloads():
    history = llm.ChatContext()
    history.add_message(
        role="user",
        content=[
            "Look at this",
            llm.ImageContent(image="https://example.com/image.png"),
        ],
    )
    assert bounded_history(history).items[0].content == ["Look at this"]
