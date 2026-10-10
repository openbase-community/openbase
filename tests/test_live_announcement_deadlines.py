"""Announcement deadlines distinguish missing startup from continuous playback."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import llm

from openbase_coder_cli.livekit_agent.live_characters import LiveCharacterController
from openbase_coder_cli.livekit_agent.packets import AnnouncerMessage


@pytest.mark.parametrize("ending", ["no_start", "stalled_playback", "caller_interrupt"])
async def test_announcement_deadlines_and_interruption_restore_conversation(
    monkeypatch, caplog, ending
):
    import openbase_coder_cli.livekit_agent.live_characters as module

    # Scale only production deadlines. The controller and cleanup remain real.
    timeout, wait_for = asyncio.timeout, asyncio.wait_for
    monkeypatch.setattr(
        module.asyncio, "timeout", lambda delay: timeout(0.05 if delay == 45 else delay)
    )

    async def scaled_wait_for(awaitable, timeout):
        return await wait_for(awaitable, 0.01 if timeout == 12 else timeout)

    monkeypatch.setattr(module.asyncio, "wait_for", scaled_wait_for)
    interrupted = asyncio.get_running_loop().create_future()
    interrupted.set_result(None)
    session = SimpleNamespace(
        current_agent=SimpleNamespace(chat_ctx=llm.ChatContext()),
        agent_state="listening",
        user_state="listening",
        output=Mock(audio_enabled=True),
        interrupt=Mock(return_value=interrupted),
    )
    router, ledger = Mock(), Mock()
    controller = LiveCharacterController(
        session=session,
        bridge=Mock(),
        router=router,
        model_factory=Mock(),
        instructions=Mock(),
        on_error=AsyncMock(),
        ledger=ledger,
    )
    monkeypatch.setattr(
        module,
        "agent_voice_identity",
        lambda _: SimpleNamespace(
            gpt_live_voice="cedar", voice_name="Agent", voice_id="agent"
        ),
    )
    live = Mock()

    async def replace(**kwargs):
        kwargs["on_enter"](live)
        return SimpleNamespace(duplex_session=live)

    controller._replace = replace
    controller._conversation = AsyncMock()

    def speak(*args, **kwargs):
        if ending == "no_start":
            return
        session.agent_state = "speaking"
        controller.state_changed(SimpleNamespace(new_state="speaking"))
        if ending == "caller_interrupt":
            asyncio.get_running_loop().call_soon(
                controller.user_state_changed, SimpleNamespace(new_state="speaking")
            )

    live.append_instructions.side_effect = speak
    await controller._play_announcement(
        AnnouncerMessage("bounded", "Work is complete.", "agent", "Agent")
    )
    controller._conversation.assert_awaited_once()
    ledger.mark_cancelled.assert_called_once()
    ledger.mark_live_audio_finished.assert_not_called()
    assert not controller.announcing
    assert controller._record is None
    router.transfer_to_thread.assert_not_called()
    assert ("live_character_announcement_timeout" in caplog.text) == (
        ending != "caller_interrupt"
    )
