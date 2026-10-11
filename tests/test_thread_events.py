"""Durable non-turn transcript events (voice route moves)."""

from __future__ import annotations

import json

from openbase_coder_cli import thread_events
from openbase_coder_cli.openbase_coder_cli_app.thread_metadata import (
    annotate_thread_payload,
)


def test_events_are_appended_bounded_and_listed_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    for index in range(thread_events.MAX_THREAD_EVENTS + 5):
        thread_events.record_thread_event(
            "s_1", kind="voice_route", text=f"event {index}"
        )
    events = thread_events.list_thread_events("s_1")
    assert len(events) == thread_events.MAX_THREAD_EVENTS
    assert events[0]["text"] == "event 5"
    assert events[-1]["text"] == f"event {thread_events.MAX_THREAD_EVENTS + 4}"
    assert events[-1]["event_id"].startswith("evt-")
    assert events[-1]["at"].endswith("+00:00")
    assert thread_events.list_thread_events("missing") == []
    assert thread_events.list_thread_events("") == []


def test_voice_route_events_land_in_both_transcripts(tmp_path, monkeypatch):
    """2026-10-10: a Dispatcher transcript ended on "Connected to Cooper." with
    no trace of the return; now both sides record the move."""
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    thread_events.record_voice_route_events(
        action="transfer_to_thread",
        dispatcher_thread_id="s_dispatcher",
        target_thread_id="s_cooper",
        target_label="Cooper",
    )
    thread_events.record_voice_route_events(
        action="exit_to_dispatch",
        dispatcher_thread_id="s_dispatcher",
        target_thread_id="s_cooper",
        target_label="Cooper",
    )
    dispatcher = [e["text"] for e in thread_events.list_thread_events("s_dispatcher")]
    cooper = [e["text"] for e in thread_events.list_thread_events("s_cooper")]
    assert dispatcher == [
        "Call transferred to Cooper.",
        "Back with the Dispatcher, from Cooper.",
    ]
    assert cooper == [
        "Call transferred here from the Dispatcher.",
        "Call returned to the Dispatcher.",
    ]
    first = thread_events.list_thread_events("s_dispatcher")[0]
    assert first["kind"] == "voice_route"
    assert first["action"] == "transfer_to_thread"
    assert first["counterpart_thread_id"] == "s_cooper"
    # A side without a thread id is skipped, an unknown action records nothing.
    thread_events.record_voice_route_events(
        action="exit_to_dispatch",
        dispatcher_thread_id="",
        target_thread_id="s_x",
        target_label=None,
    )
    assert [e["text"] for e in thread_events.list_thread_events("s_x")] == [
        "Call returned to the Dispatcher."
    ]
    thread_events.record_voice_route_events(
        action="nope",
        dispatcher_thread_id="s_d2",
        target_thread_id="s_t2",
        target_label="A",
    )
    assert thread_events.list_thread_events("s_d2") == []


def test_malformed_lines_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    path = thread_events._events_path("s_bad")
    path.parent.mkdir(parents=True)
    path.write_text(
        "not json\n"
        + json.dumps({"kind": "voice_route"})
        + "\n"
        + json.dumps({"kind": "voice_route", "text": "kept"})
        + "\n"
    )
    assert [e["text"] for e in thread_events.list_thread_events("s_bad")] == ["kept"]


def test_thread_detail_payload_carries_events_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    thread_events.record_thread_event(
        "s_1", kind="voice_route", text="Call transferred to Cooper."
    )
    listed = annotate_thread_payload({"thread_id": "s_1", "directory": "/w"})
    assert "events" not in listed
    detail = annotate_thread_payload(
        {"thread_id": "s_1", "directory": "/w"}, with_events=True
    )
    assert [e["text"] for e in detail["events"]] == ["Call transferred to Cooper."]
