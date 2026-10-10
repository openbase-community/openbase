import asyncio
from types import SimpleNamespace

import pytest
from super_agents.agent_store import Store

from openbase_coder_cli.agent_announcements.claude import ManagedAnnouncements
from openbase_coder_cli.agent_announcements.ledger import AnnouncementLedger
from openbase_coder_cli.agent_announcements.protocol import (
    MAX_CORRECTIONS,
    TOOL_NAME,
    AnnouncementProtocol,
)


@pytest.fixture
def worker(tmp_path):
    store = Store(tmp_path / "agents.sqlite3")
    session = store.create_session(
        name="file-review",
        agent_name="Rowan",
        cwd=str(tmp_path),
        developer_instructions="You are an Openbase Super Agent.",
    )
    turn = store.create_turn(
        session.id, "Inspect the current file and explain the result.", status="running"
    )
    store.update_session(
        session.id, active_turn_id=turn.id, last_turn_id=turn.id, status="running"
    )
    receipts = []

    async def publish(session, text, message_id, room):
        receipts.append((session.id, text, message_id, room))
        return {"status": "published", "message_id": message_id, "room_name": room}

    async def room():
        return "room-original"

    ledger = AnnouncementLedger(tmp_path / "announcements.sqlite3")
    ledger.mark_delegated(turn.id, "dispatcher-parent")
    protocol = AnnouncementProtocol(store, ledger, publish=publish, resolve_room=room)
    return SimpleNamespace(
        store=store,
        session=session,
        turn=turn,
        receipts=receipts,
        protocol=protocol,
        ledger=ledger,
        publish=publish,
        room=room,
    )


def set_prompt(w, prompt):
    w.store.update_turn(w.turn.id, status="cancelled")
    w.turn = w.store.create_turn(w.session.id, prompt, status="running")
    w.store.update_session(
        w.session.id, active_turn_id=w.turn.id, last_turn_id=w.turn.id
    )
    w.ledger.mark_delegated(w.turn.id, "dispatcher-parent")


def event(name="Read", tool_id="read-1"):
    return {"tool_name": name, "tool_use_id": tool_id, "tool_input": {}}


async def begin(w, **kwargs):
    return await w.protocol.announce(
        w.session.id, {"phase": "begin", "delivery": "audible", **kwargs}
    )


async def finish(w, summary="Rowan: the file contains the requested value."):
    return await w.protocol.announce(
        w.session.id, {"phase": "finish", "summary": summary}
    )


async def terminal(w):
    await w.protocol.validate_turn_result(
        w.session, w.turn, SimpleNamespace(is_error=False)
    )


async def test_read_only_skip_is_blocked_then_worker_can_complete_protocol(worker):
    w = worker
    denied = await w.protocol.before_tool(w.session.id, event())
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not w.receipts
    await begin(w)
    assert len(w.receipts) == 1 and "Rowan" in w.receipts[0][1]
    assert await w.protocol.before_tool(w.session.id, event()) == {}
    with pytest.raises(ValueError, match="still running"):
        await finish(w)
    await w.protocol.after_tool(w.session.id, event())
    await finish(w)
    assert len(w.receipts) == 1  # finish is intent, not premature speech
    assert await w.protocol.stop(w.session.id) == {}
    await terminal(w)
    assert len(w.receipts) == 2
    assert w.receipts[1][3] == "room-original"
    await terminal(w)
    assert len(w.receipts) == 2  # terminal callback retry is idempotent


async def test_explicit_quiet_never_resolves_room_or_publishes(worker):
    w = worker
    set_prompt(w, "Inspect the file silently. No notifications.")

    async def forbidden():
        pytest.fail("Quiet task must not query LiveKit")

    w.protocol.resolve_room = forbidden
    await begin(w, delivery="quiet", quiet_request="No notifications.")
    await w.protocol.before_tool(w.session.id, event())
    await w.protocol.after_tool(w.session.id, event())
    await finish(w)
    await terminal(w)
    assert not w.receipts


async def test_quiet_requires_user_evidence_and_survives_steer(worker):
    w = worker
    with pytest.raises(ValueError, match="explicit quiet"):
        await begin(w, delivery="quiet", quiet_request="Invented restriction")
    set_prompt(w, "Read this quietly, without announcements.")
    await begin(w, delivery="quiet", quiet_request="without announcements")
    w.store.append_turn_steer(w.turn.id, "Also inspect the second file.")
    denied = await w.protocol.before_tool(w.session.id, event())
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    with pytest.raises(ValueError, match="Quiet intent persists"):
        await begin(w)
    await begin(w, delivery="quiet", quiet_request="without announcements")
    await finish(w)
    await terminal(w)
    assert not w.receipts


async def test_new_steer_invalidates_completion_and_can_explicitly_resume_speech(
    worker,
):
    w = worker
    set_prompt(w, "Do the check silently.")
    await begin(w, delivery="quiet", quiet_request="silently")
    await finish(w)
    w.store.append_turn_steer(w.turn.id, "You may speak now. Check the result again.")
    with pytest.raises(RuntimeError, match="without a verified"):
        await terminal(w)
    await begin(w, resume_request="You may speak now.")
    await finish(w, "Rowan: the second check passed.")
    await terminal(w)
    assert len(w.receipts) == 2
    assert "second check" in w.receipts[-1][1]
    assert w.receipts[-1][-1] == "room-original"


async def test_parallel_work_after_finish_invalidates_old_summary(worker):
    w = worker
    await begin(w)
    await finish(w, "Old result.")
    await w.protocol.before_tool(w.session.id, event("Bash", "shell-2"))
    assert (await w.protocol.stop(w.session.id))["decision"] == "block"
    await w.protocol.after_tool(w.session.id, event("Bash", "shell-2"))
    with pytest.raises(RuntimeError, match="without a verified"):
        await terminal(w)
    await finish(w, "Rowan: the verified new result.")
    await terminal(w)
    assert all(text != "Old result." for _, text, _, _ in w.receipts)


async def test_cancelled_turn_cannot_publish_completion(worker):
    w = worker
    await begin(w)
    await finish(w)
    w.store.update_turn(w.turn.id, status="cancelled")
    with pytest.raises(RuntimeError, match="no longer active"):
        await terminal(w)
    assert len(w.receipts) == 1


async def test_direct_voice_never_adds_default_announcements(worker):
    w = worker
    set_prompt(w, "<voice>Explain the last result.</voice>")
    assert await w.protocol.before_tool(w.session.id, event()) == {}
    await w.protocol.after_tool(w.session.id, event())
    assert await w.protocol.stop(w.session.id) == {}
    await terminal(w)
    assert not w.receipts


async def test_introduction_survives_client_restart_and_later_turn(worker):
    w = worker
    await begin(w)
    w.store.update_turn(w.turn.id, status="completed")
    next_turn = w.store.create_turn(
        w.session.id, "Verify a different task.", status="running"
    )
    w.store.update_session(
        w.session.id, active_turn_id=next_turn.id, last_turn_id=next_turn.id
    )
    w.ledger.mark_delegated(next_turn.id, "dispatcher-parent")
    w.protocol = AnnouncementProtocol(
        w.store, w.ledger, publish=w.publish, resolve_room=w.room
    )
    await begin(w)
    assert len(w.receipts) == 1
    await finish(w)
    await w.protocol.validate_turn_result(
        w.session, next_turn, SimpleNamespace(is_error=False)
    )
    assert len(w.receipts) == 2


async def test_missing_protocol_has_bounded_corrections_and_never_succeeds(worker):
    w = worker
    for _ in range(MAX_CORRECTIONS):
        response = await w.protocol.stop(w.session.id)
    assert response["continue_"] is False
    with pytest.raises(RuntimeError, match="bounded corrections"):
        await terminal(w)
    assert not w.receipts


async def test_ambiguous_failed_intro_is_not_repeated_or_claimed(worker):
    w = worker
    attempts = []

    async def failure(*args):
        attempts.append(args)
        raise TimeoutError("Submission acknowledgement lost")

    w.protocol.publish = failure
    with pytest.raises(TimeoutError):
        await begin(w)
    with pytest.raises(RuntimeError, match="no automatic repeat"):
        await begin(w)
    assert len(attempts) == 1


async def test_two_controllers_claim_one_introduction(worker):
    w = worker
    other = AnnouncementProtocol(
        w.store, w.ledger, publish=w.publish, resolve_room=w.room
    )
    results = await asyncio.gather(
        begin(w),
        other.announce(w.session.id, {"phase": "begin", "delivery": "audible"}),
        return_exceptions=True,
    )
    assert len(w.receipts) == 1
    assert any(isinstance(result, dict) for result in results)


def test_real_sdk_options_preserve_hooks_servers_and_attach_protocol(
    worker, monkeypatch
):
    import claude_agent_sdk as sdk

    from openbase_coder_cli.agent_announcements import claude

    monkeypatch.setattr(
        claude,
        "get_livekit_voice_route_state",
        lambda: SimpleNamespace(dispatcher_thread_id="another-id"),
    )
    sentinel = sdk.HookMatcher(hooks=[])
    options = sdk.ClaudeAgentOptions(
        hooks={"Stop": [sentinel]}, mcp_servers={"existing": {"command": "example"}}
    )
    managed = ManagedAnnouncements(worker.store, protocol=worker.protocol)
    managed.configure_session(worker.session, options, sdk)
    assert options.hooks["Stop"][0] is sentinel
    assert len(options.hooks["Stop"]) == 2
    assert "existing" in options.mcp_servers
    assert options.mcp_servers["openbase_agent"]["type"] == "sdk"
    assert TOOL_NAME.endswith("task_announcement")


def test_canonical_dispatcher_is_exempt_independent_of_title(worker, monkeypatch):
    import claude_agent_sdk as sdk

    from openbase_coder_cli.agent_announcements import claude

    monkeypatch.setattr(
        claude,
        "get_livekit_voice_route_state",
        lambda: SimpleNamespace(dispatcher_thread_id=worker.session.id),
    )
    options = sdk.ClaudeAgentOptions()
    ManagedAnnouncements(worker.store, protocol=worker.protocol).configure_session(
        worker.session, options, sdk
    )
    assert not options.hooks
    assert not options.mcp_servers


async def test_direct_text_has_no_delegation_origin_or_default_announcements(worker):
    w = worker
    turn = w.store.create_turn(
        w.session.id, "Explain the last result.", status="running"
    )
    w.store.update_session(w.session.id, active_turn_id=turn.id, last_turn_id=turn.id)
    assert (await begin(w))["status"] == "direct_reply"
    assert await w.protocol.before_tool(w.session.id, event()) == {}
    assert await w.protocol.stop(w.session.id) == {}
    await w.protocol.validate_turn_result(
        w.session, turn, SimpleNamespace(is_error=False)
    )
    assert not w.receipts


async def test_mcp_delegation_marks_first_turn_before_scheduled_worker_runs(worker):
    from super_agents.app_models import LabelQueryInput

    from openbase_coder_cli.agent_announcements.delegation import DelegatingClient

    w = worker
    observed = []
    tasks = []

    class Client:
        def _resolve_session(self, query):
            assert query.thread_id == w.session.id
            return w.store.get_session(w.session.id)

        async def start_turn_by_label(self, query, turn_input):
            turn = w.store.create_turn(
                w.session.id, turn_input["prompt"], status="running"
            )

            async def run():
                observed.append(w.ledger.is_delegated(turn.id))

            tasks.append(asyncio.create_task(run()))
            return {"turnId": turn.id}

    proxy = DelegatingClient(Client(), w.ledger, "actual-parent")
    result = await proxy.start_turn_by_label(
        LabelQueryInput(thread_id=w.session.id), {"prompt": "An arbitrary task."}
    )
    await asyncio.gather(*tasks)
    assert observed == [True]
    assert w.ledger.read("origin:" + result["turnId"])["parent_id"] == "actual-parent"


async def test_stale_context_does_not_roll_back_new_steer(worker):
    w = worker
    await begin(w)
    old = await w.protocol.context(w.session.id)
    w.store.append_turn_steer(w.turn.id, "Use the second file instead.")
    await w.protocol.before_tool(w.session.id, event())
    with pytest.raises(RuntimeError, match="stale announcement"):
        await w.protocol._edit(old, lambda state, task: task.update(delivery="audible"))
    state = w.ledger.read(w.session.id)
    assert state["turn"]["revision"] != old.revision
    assert state["turn"]["delivery"] is None


async def test_late_submission_receipt_does_not_undo_steer(worker):
    w = worker
    await begin(w)
    await finish(w)
    old = await w.protocol.context(w.session.id)
    w.store.append_turn_steer(
        w.turn.id, "Stop that result and inspect a different file."
    )
    await w.protocol.before_tool(w.session.id, event())
    await w.protocol._record_submission(old, "completion", "old-id", status="submitted")
    assert "completion" not in w.ledger.read(w.session.id)["turn"]


async def test_retry_begin_preserves_original_room_and_prepared_result(worker):
    w = worker
    await begin(w)
    await finish(w)

    async def different_room():
        return "new-call"

    w.protocol.resolve_room = different_room
    await begin(w)
    await terminal(w)
    assert len(w.receipts) == 2
    assert w.receipts[-1][-1] == "room-original"


async def test_managed_playback_guard_rejects_cancel_steer_and_unknown_receipt(worker):
    from openbase_coder_cli.agent_announcements.playback import speech_guard

    w = worker
    await begin(w)
    message_id = w.receipts[0][2]
    guard = await speech_guard(message_id, ledger=w.ledger)
    assert guard.current()
    w.store.append_turn_steer(w.turn.id, "No announcements. Keep this quiet.")
    assert not guard.current()
    unknown = await speech_guard("announcer-managed-" + "0" * 32, ledger=w.ledger)
    assert not unknown.current()
    assert await speech_guard("announcer-ordinary", ledger=w.ledger) is None
    w.store.update_turn(w.turn.id, status="cancelled")
    assert not guard.current()


async def test_managed_playback_monitor_interrupts_and_cleans_up(worker):
    from openbase_coder_cli.agent_announcements.playback import (
        monitor_speech,
        speech_guard,
    )

    w = worker
    await begin(w)
    guard = await speech_guard(w.receipts[0][2], ledger=w.ledger)
    interrupted = asyncio.Event()
    async with monitor_speech(guard, interrupted.set):
        w.store.update_turn(w.turn.id, status="cancelled")
        await asyncio.wait_for(interrupted.wait(), timeout=1)
    assert interrupted.is_set()


async def test_failed_introduction_cannot_be_hidden_by_finish(worker):
    w = worker

    async def failed(*args):
        raise TimeoutError("No acknowledgement")

    w.protocol.publish = failed
    with pytest.raises(TimeoutError):
        await begin(w)
    with pytest.raises(RuntimeError, match="no submission receipt"):
        await finish(w)
    with pytest.raises(RuntimeError, match="without a verified"):
        await terminal(w)


async def test_quiet_task_can_complete_without_any_work_tools(worker):
    w = worker
    set_prompt(w, "Explain what you already know. Text only.")
    await begin(w, delivery="quiet", quiet_request="Text only.")
    await finish(w, "Rowan: explained the existing context without changing files.")
    await terminal(w)
    assert not w.receipts


def test_old_backend_fails_with_explicit_compatibility_message():
    from openbase_coder_cli.agent_announcements.claude import managed_claude_client

    class OldBackend:
        def __init__(self, **kwargs):
            pytest.fail("An unsupported backend must not be constructed")

    with pytest.raises(RuntimeError, match="session extensions"):
        managed_claude_client(OldBackend)


async def test_managed_factory_real_sdk_tool_and_hooks_complete_same_protocol(
    worker, monkeypatch
):
    import claude_agent_sdk as sdk
    import mcp.types as types

    from openbase_coder_cli.agent_announcements import claude

    w = worker
    monkeypatch.setattr(
        claude,
        "get_livekit_voice_route_state",
        lambda: SimpleNamespace(dispatcher_thread_id="parent"),
    )
    client = claude.managed_claude_client(store=w.store)
    managed = client._configure_session.__self__
    managed.protocol = w.protocol
    options = sdk.ClaudeAgentOptions(
        mcp_servers={"super-agents": {"command": "super-agents-mcp"}}
    )
    client._configure_session(w.session, options, sdk)
    assert options.mcp_servers["super-agents"]["type"] == "sdk"
    server = options.mcp_servers["openbase_agent"]["instance"]

    async def invoke(arguments):
        request = types.CallToolRequest(
            params=types.CallToolRequestParams(
                name="task_announcement", arguments=arguments
            )
        )
        return (await server.request_handlers[types.CallToolRequest](request)).root

    pre = options.hooks["PreToolUse"][-1].hooks[0]
    post = options.hooks["PostToolUse"][-1].hooks[0]
    stop = options.hooks["Stop"][-1].hooks[0]
    assert (await pre(event(), "read-1", None))["hookSpecificOutput"][
        "permissionDecision"
    ] == "deny"
    assert not (await invoke({"phase": "begin", "delivery": "audible"})).isError
    assert await pre(event(), "read-1", None) == {}
    assert (await invoke({"phase": "finish", "summary": "Premature result."})).isError
    await post(event(), "read-1", None)
    assert (await stop({}, None, None))["decision"] == "block"
    assert not (
        await invoke(
            {"phase": "finish", "summary": "Rowan: inspected the requested file."}
        )
    ).isError
    assert await stop({}, None, None) == {}
    assert len(w.receipts) == 1
    await client._validate_turn_result(
        w.session, w.turn, SimpleNamespace(is_error=False)
    )
    assert len(w.receipts) == 2


async def test_worker_can_preserve_explicit_quiet_choice_for_later_followup(worker):
    w = worker
    set_prompt(w, "Keep this conversation silent.")
    await begin(w, delivery="quiet", quiet_request="Keep this conversation silent.")
    await finish(w)
    await terminal(w)
    set_prompt(w, "Explain the second file too.")
    await begin(w, delivery="quiet", quiet_request="Keep this conversation silent.")
    await finish(w)
    await terminal(w)
    assert not w.receipts


async def test_cancel_before_submission_releases_unattempted_intro_claim(worker):
    w = worker
    context = await w.protocol.context(w.session.id)
    w.ledger.edit(
        w.session.id, lambda state: state["intro"].update(status="submitting")
    )
    w.store.update_turn(w.turn.id, status="cancelled")
    with pytest.raises(RuntimeError, match="stale announcement"):
        await w.protocol._submit(
            context, "intro", "Hi, I'm Rowan.", "announcer-managed-" + "1" * 32
        )
    assert w.ledger.read(w.session.id)["intro"]["status"] == "not_submitted"
    assert not w.receipts
    set_prompt(w, "A new delegated task.")
    await begin(w)
    assert len(w.receipts) == 1
