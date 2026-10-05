from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

from click.testing import CliRunner
from super_agents.state import SessionRecord, StateFile, TurnSummary, write_state_file

from openbase_coder_cli.cli import main

problems_service = importlib.import_module("openbase_coder_cli.problems_service")


def _write_state(path: Path, *sessions: SessionRecord) -> Path:
    state = StateFile(sessions={session.thread_id: session for session in sessions})
    write_state_file(path, state)
    return path


def _session(
    thread_id: str,
    *,
    updated_at: str,
    label: str | None = None,
    last_event_at: str | None = None,
    prompt_preview: str | None = None,
) -> SessionRecord:
    turns = None
    if prompt_preview is not None:
        turns = {
            "turn-1": TurnSummary(
                turn_id="turn-1",
                status="completed",
                started_at=updated_at,
                updated_at=updated_at,
                prompt_preview=prompt_preview,
            )
        }
    return SessionRecord(
        thread_id=thread_id,
        updated_at=updated_at,
        label=label,
        last_event_at=last_event_at,
        last_turn_id="turn-1" if turns else None,
        turns=turns,
    )


def test_resolve_target_session_picks_most_recent(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _session("older", updated_at="2026-09-20T10:00:00.000Z"),
        _session(
            "newest",
            updated_at="2026-09-21T09:00:00.000Z",
            last_event_at="2026-09-22T09:00:00.000Z",
        ),
    )

    thread_id, session = problems_service.resolve_target_session(state_path=state_path)

    assert thread_id == "newest"
    assert session is not None and session.thread_id == "newest"


def test_resolve_target_session_honors_explicit_thread_id(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _session("older", updated_at="2026-09-20T10:00:00.000Z"),
        _session("newest", updated_at="2026-09-22T10:00:00.000Z"),
    )

    thread_id, session = problems_service.resolve_target_session(
        "older", state_path=state_path
    )

    assert thread_id == "older"
    assert session is not None and session.thread_id == "older"


def test_resolve_target_session_raises_without_activity(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(state_path)

    try:
        problems_service.resolve_target_session(state_path=state_path)
    except LookupError as exc:
        assert "No thread activity" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected LookupError")


def test_capture_uses_app_server_messages(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _session("t1", updated_at="2026-09-22T10:00:00.000Z", label="dispatcher"),
    )

    async def reader(_thread_id: str) -> list[dict[str, str]]:
        return [
            {"role": "user", "text": "first prompt"},
            {"role": "assistant", "text": "first reply"},
            {"role": "user", "text": "the broken prompt"},
            {"role": "assistant", "text": "a bad answer"},
            {"role": "tool", "text": '{"status": "failed"}'},
        ]

    record = asyncio.run(
        problems_service.acapture_problem(
            note="  it went wrong  ",
            reported_by="s_agent",
            state_path=state_path,
            messages_reader=reader,
            now=datetime(2026, 9, 22, 10, 30, tzinfo=UTC),
        )
    )

    assert record.user_message.text == "the broken prompt"
    assert record.user_message.source == "app_server"
    assert record.note == "it went wrong"
    assert record.reported_by == "s_agent"
    assert record.thread.label == "dispatcher"
    # Interaction tail starts at the latest user message and keeps the fallout.
    assert record.interaction[0] == {"role": "user", "text": "the broken prompt"}
    assert [m["role"] for m in record.interaction] == ["user", "assistant", "tool"]
    assert record.id.startswith("prob-20260922T103000Z-")


def test_capture_falls_back_to_state_preview(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _session(
            "t1",
            updated_at="2026-09-22T10:00:00.000Z",
            prompt_preview="cached preview prompt",
        ),
    )

    async def failing_reader(_thread_id: str) -> list[dict[str, str]]:
        raise RuntimeError("app server offline")

    record = asyncio.run(
        problems_service.acapture_problem(
            state_path=state_path,
            messages_reader=failing_reader,
        )
    )

    assert record.user_message.text == "cached preview prompt"
    assert record.user_message.source == "state_preview"
    assert record.interaction == []


def test_capture_raises_when_no_message_anywhere(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _session("t1", updated_at="2026-09-22T10:00:00.000Z"),
    )

    async def empty_reader(_thread_id: str) -> list[dict[str, str]]:
        return []

    try:
        asyncio.run(
            problems_service.acapture_problem(
                state_path=state_path,
                messages_reader=empty_reader,
            )
        )
    except LookupError as exc:
        assert "No user message" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected LookupError")


def test_write_list_and_resolve_round_trip(monkeypatch, tmp_path: Path) -> None:
    problems_dir = tmp_path / "problems"
    monkeypatch.setattr(problems_service, "PROBLEMS_DIR", problems_dir)

    record = problems_service.ProblemRecord(
        id="prob-20260922T103000Z-abcdef01",
        created_at="2026-09-22T10:30:00+00:00",
        thread=problems_service.ThreadMetadata(thread_id="t1", label="dispatcher"),
        user_message=problems_service.CapturedMessage(
            text="broken prompt", source="app_server"
        ),
    )
    path = problems_service.write_problem_record(record)

    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["user_message"]["text"] == "broken prompt"
    # Empty optional fields are dropped from the stored record.
    assert "interaction" not in on_disk

    listed = problems_service.list_problem_records()
    assert len(listed) == 1 and listed[0]["id"] == record.id

    resolved = problems_service.resolve_problem_record(record.id)
    assert resolved["id"] == record.id


def test_report_issue_cli_end_to_end(monkeypatch, tmp_path: Path) -> None:
    problems_dir = tmp_path / "problems"
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(problems_service, "PROBLEMS_DIR", problems_dir)
    monkeypatch.setenv("SUPER_AGENTS_STATE_FILE", str(state_path))
    _write_state(
        state_path,
        _session(
            "t1",
            updated_at="2026-09-22T10:00:00.000Z",
            label="dispatcher",
            prompt_preview="cached broken prompt",
        ),
    )

    # No app server in tests: the default reader fails to connect and the
    # capture falls back to the state preview.
    runner = CliRunner()
    result = runner.invoke(main, ["report", "issue", "--note", "voice call dropped"])
    assert result.exit_code == 0, result.output
    assert "Recorded problem prob-" in result.output
    assert "cached broken prompt" in result.output

    listing = runner.invoke(main, ["report", "list", "--json"])
    assert listing.exit_code == 0, listing.output
    payload = json.loads(listing.output)
    assert payload["count"] == 1
    assert payload["items"][0]["note"] == "voice call dropped"
    assert payload["items"][0]["thread"]["label"] == "dispatcher"
