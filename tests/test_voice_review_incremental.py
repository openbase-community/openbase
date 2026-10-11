import pytest
from test_agent_announcement_protocol import begin, event, finish, terminal
from test_live_delegation import _make_bridge, _settle


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


@pytest.mark.asyncio
async def test_failed_intro_does_not_skip_tool_tracking_or_reuse_stale_finish(worker):
    w = worker

    async def unavailable(*args):
        raise TimeoutError("announcement endpoint temporarily unavailable")

    w.protocol.publish = unavailable
    with pytest.raises(TimeoutError):
        await begin(w)
    await finish(w, "Rowan: the tests passed.")
    assert (
        await w.protocol.before_tool(w.session.id, event("Bash", "rerun-tests")) == {}
    )
    await w.protocol.after_tool(w.session.id, event("Bash", "rerun-tests"))
    # The additional work invalidates the previously prepared summary.
    w.protocol.publish = w.publish
    await terminal(w)
    assert not w.receipts, "announced old success after untracked subsequent work"
