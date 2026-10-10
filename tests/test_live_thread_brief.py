"""The voice session learns what its thread recently discussed."""

from __future__ import annotations

import asyncio

from test_live_delegation import _make_bridge, _settle

from openbase_coder_cli.livekit_agent import live_thread_brief as brief
from openbase_coder_cli.livekit_agent.live_thread_brief import (
    ThreadExchange,
    exchanges_from_thread_payload,
    thread_brief_note,
)


def test_exchanges_come_from_turns_with_voice_tags_and_system_notes_stripped():
    payload = {
        "turn_history": [
            {
                "prompt": "<voice>What is seven times eight?</voice>",
                "accumulated_output": "Fifty-six.",
            },
            {
                "prompt": "[Openbase system note: x]\n\nList the agents",
                "accumulated_output": "Cooper, Alice.",
            },
            {"prompt": "", "accumulated_output": ""},
        ],
        "current_turn": {"prompt": "Running one", "accumulated_output": None},
    }
    assert exchanges_from_thread_payload(payload) == [
        ThreadExchange("What is seven times eight?", "Fifty-six."),
        ThreadExchange("List the agents", "Cooper, Alice."),
        ThreadExchange("Running one", ""),
    ]


def test_brief_note_is_bounded_newest_kept_and_empty_when_nothing_to_tell():
    assert thread_brief_note([], agent_label="Cooper") is None
    exchanges = [
        ThreadExchange(f"question {i} " + "x" * 300, "answer " + "y" * 300)
        for i in range(10)
    ]
    note = thread_brief_note(exchanges, agent_label="Cooper")
    assert note is not None
    assert len(note) <= brief.BRIEF_MAX_CHARS
    assert "question 9" in note and "question 0" not in note
    assert note.startswith("Context only, not to be read aloud")
    assert "…" in note
    single = thread_brief_note([ThreadExchange("hi", "")], agent_label="Dispatcher")
    assert single is not None and "Caller: hi | Dispatcher: (no reply yet)" in single


async def test_bridge_briefs_the_active_thread_at_session_start_and_after_a_handoff():
    """2026-10-10 (Gabe): a call in an existing conversation must not start
    from zero; the voice session gets the thread's recent exchanges."""
    fetched: list[str] = []

    async def fetcher(thread_id: str):
        fetched.append(thread_id)
        return [ThreadExchange("Fix the login bug", "Done, the fix is in auth.py.")]

    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        thread_exchange_fetcher=fetcher
    )
    try:
        bridge.brief_active_thread()
        await _settle()
        assert fetched == ["dispatcher-thread"]
        notes = live.of("thinking", None)
        assert len(notes) == 1
        assert "Fix the login bug" in notes[0] and "auth.py" in notes[0]
        assert "Caller:" in notes[0] and "the dispatcher:" in notes[0]
        # A character start (transfer or return) briefs again for the new route.
        bridge.on_character_session_started()
        await _settle()
        assert fetched == ["dispatcher-thread", "dispatcher-thread"]
        assert len(live.of("thinking", None)) >= 2
    finally:
        await bridge.aclose()


async def test_bridge_brief_failure_is_silent_and_an_empty_thread_adds_nothing():
    async def failing(thread_id: str):
        raise OSError("api down")

    bridge, live, *_ = _make_bridge(thread_exchange_fetcher=failing)
    try:
        bridge.brief_active_thread()
        await _settle()
        assert live.of("thinking", None) == []
    finally:
        await bridge.aclose()

    async def empty(thread_id: str):
        return []

    bridge, live, *_ = _make_bridge(thread_exchange_fetcher=empty)
    try:
        bridge.brief_active_thread()
        await _settle()
        assert live.of("thinking", None) == []
    finally:
        await bridge.aclose()


async def test_fetcher_runs_off_the_loop(monkeypatch):
    monkeypatch.setattr(
        brief,
        "fetch_thread_exchanges_sync",
        lambda thread_id: [ThreadExchange(thread_id, "ok")],
    )
    assert await brief.fetch_thread_exchanges("s_1") == [ThreadExchange("s_1", "ok")]
    await asyncio.sleep(0)


def test_fetcher_uses_the_configured_local_server_address(monkeypatch):
    """Maritime serves the local API on 18789, not 7999 (2026-10-10)."""
    monkeypatch.setenv("OPENBASE_CODER_CLI_PORT", "18789")
    monkeypatch.delenv("OPENBASE_CODER_CLI_SERVER_URL", raising=False)
    monkeypatch.setattr(brief, "get_local_api_token", lambda: "tok")
    seen = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return (
                b'{"turn_history": [{"prompt": "hi", "accumulated_output": "hello"}]}'
            )

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return _Response()

    monkeypatch.setattr(brief.urllib.request, "urlopen", fake_urlopen)
    assert brief.fetch_thread_exchanges_sync("s_1") == [ThreadExchange("hi", "hello")]
    assert seen["url"] == "http://127.0.0.1:18789/api/threads/s_1/"
    assert seen["auth"] == "Bearer tok"
