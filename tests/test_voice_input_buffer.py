from openbase_coder_cli.livekit_agent.voice_input_buffer import VoiceInputBuffer


def test_cancelled_unsubmitted_fragments_merge_once_and_cannot_double_submit():
    buffer = VoiceInputBuffer()
    first = buffer.add("Count one to thirty.", "dispatcher")
    continued = buffer.add("Then say amber lantern complete.", "dispatcher")
    assert continued.prompt == "Count one to thirty. Then say amber lantern complete."
    assert not buffer.consume(first, "dispatcher")
    assert buffer.consume(continued, "dispatcher")
    assert not buffer.consume(continued, "dispatcher")
    assert buffer.add("Next question.", "dispatcher").prompt == "Next question."


def test_cumulative_transcripts_and_route_changes_never_repeat_old_input():
    buffer = VoiceInputBuffer()
    buffer.add("Build Tetris", "dispatcher")
    combined = buffer.add("Build Tetris with a score", "dispatcher")
    assert combined.prompt == "Build Tetris with a score"
    repeated = buffer.add("Build Tetris with a score.", "dispatcher")
    assert repeated.prompt == "Build Tetris with a score"
    assert not buffer.consume(repeated, "super-agent")
    assert buffer.add("Make it purple", "super-agent").prompt == "Make it purple"


def test_late_merged_tail_does_not_repeat_already_buffered_fragments():
    buffer = VoiceInputBuffer()
    fragments = ["Start two agents.", "Build Tetris in Pine.", "Build chess in Birch.",
        "Have both introduce themselves.", "Write result markdown.", "Keep work independent."]
    buffer.add(fragments[0], "dispatcher")
    buffer.add(" ".join(fragments[:2]), "dispatcher")
    buffer.add(fragments[2], "dispatcher")
    buffer.add(fragments[3], "dispatcher")
    buffer.add(" ".join(fragments[-2:]), "dispatcher")
    item = buffer.add(" ".join(fragments[-3:]), "dispatcher")
    assert item.prompt == " ".join(fragments)


def test_expired_unsubmitted_input_is_not_attached_to_a_new_question(monkeypatch):
    from openbase_coder_cli.livekit_agent import voice_input_buffer
    now = [1.0]
    monkeypatch.setattr(voice_input_buffer.time, "monotonic", lambda: now[0])
    buffer = VoiceInputBuffer(ttl_seconds=30)
    buffer.add("Old interrupted request", "dispatcher")
    now[0] = 32.0
    assert buffer.add("New request", "dispatcher").prompt == "New request"


def test_bridge_does_not_execute_until_quiet_and_preserves_cancelled_input():
    import asyncio
    from types import SimpleNamespace
    from openbase_coder_cli.livekit_agent.codex_llm import CodexLLMStream

    async def scenario():
        closed = asyncio.Event()
        calls = []
        buffer = VoiceInputBuffer()
        ticket = buffer.add("Build Tetris", "route")
        async def run_turn(prompt, **kwargs):
            calls.append(prompt)
            return {}
        async def wait_for_close(record, **kwargs):
            await closed.wait()
            return True
        router = SimpleNamespace(is_dispatcher_active=False, active_client=SimpleNamespace(run_turn=run_turn),
            input_buffer=buffer, route_snapshot=lambda: "route")
        ledger = SimpleNamespace(wait_for_user_turn_closed=wait_for_close, mark_cancelled=lambda *args, **kwargs: None)
        stream = SimpleNamespace(_voice_router=router, _buffered_input=ticket, _message_id="first", _event_ch=SimpleNamespace(closed=False))
        record = SimpleNamespace(prompt_len=12)
        task = asyncio.create_task(CodexLLMStream._run_accepted_prompt(stream, ticket.prompt, record, ledger))
        await asyncio.sleep(0)
        assert not calls
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        continued = buffer.add("with a score", "route")
        stream._buffered_input = continued
        closed.set()
        await CodexLLMStream._run_accepted_prompt(stream, continued.prompt, record, ledger)
        assert len(calls) == 1
        assert "Build Tetris with a score" in calls[0]
    asyncio.run(scenario())
