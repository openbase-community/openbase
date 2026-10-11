import pytest
from test_live_delegation import FakeVoiceClient, _make_bridge, _settle

from openbase_coder_cli.livekit_agent.turn_recovery import (
    TurnRecoveryFailed,
    run_with_recovery,
)


async def test_retry_does_not_submit_after_route_changes_during_backoff():
    wanted = True
    calls = []

    async def run(index):
        calls.append(index)
        if index == 0:
            raise ConnectionRefusedError("backend down")
        return {"status": "completed"}

    async def sleep(delay):
        nonlocal wanted
        wanted = False

    try:
        await run_with_recovery(
            run,
            should_continue=lambda: wanted,
            window_seconds=90,
            backoff_seconds=[3],
            sleep=sleep,
        )
    except TurnRecoveryFailed:
        pass
    assert calls == [0], "request replayed after route changed during backoff"


async def test_second_transfer_does_not_recarry_old_route_requests():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    first = FakeVoiceClient(thread_id="first-thread")
    second = FakeVoiceClient(thread_id="second-thread")
    bridge.character_route_changed = lambda action, **kw: None
    try:
        live.final("Connect me to first.")
        await _settle()
        live.final("Create the project file.")
        await _settle()
        router.transfer(first)
        bridge.notify_route_changed(action="transfer_to_thread", agent_label="First")
        await _settle()
        assert len(first.prompts) == 1
        router.transfer(second)
        bridge.notify_route_changed(action="transfer_to_thread", agent_label="Second")
        await _settle()
        assert second.prompts == [], (
            "old route request was carried again without new caller input"
        )
    finally:
        await bridge.aclose()


@pytest.mark.parametrize("word", ["Stop", "Wait", "Undo", "No"])
async def test_short_real_interruptions_are_not_discarded_as_echo(word):
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    try:
        bridge.on_agent_state_changed("listening", "speaking")
        bridge.speech_gate.agent_said("The build is proceeding successfully.")
        bridge.on_user_state_changed("listening", "speaking")
        live.final(word)
        bridge.on_user_state_changed("speaking", "listening")
        await _settle()
        assert dispatcher.prompts, (
            f"{word!r} was dropped despite not matching any agent speech"
        )
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "said,word",
    [("I will store the file now.", "Stop"), ("The main function is ready.", "Wait")],
)
async def test_real_command_is_not_a_fuzzy_clipped_echo(said, word):
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    try:
        bridge.on_agent_state_changed("listening", "speaking")
        bridge.speech_gate.agent_said(said)
        bridge.on_user_state_changed("listening", "speaking")
        live.final(word)
        bridge.on_user_state_changed("speaking", "listening")
        await _settle()
        assert dispatcher.prompts, f"{word!r} discarded over {said!r}"
    finally:
        await bridge.aclose()
