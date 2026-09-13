"""Tests for livekit_agent.stall_diagnostics (field-test finding FT-9)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from openbase_coder_cli.livekit_agent import stall_diagnostics as sd


def _write_session_log(tmp_path: Path, thread_id: str, lines: list[dict]) -> None:
    logs = tmp_path / "super-agents-claude-code" / "logs"
    logs.mkdir(parents=True)
    path = logs / f"{thread_id}.log"
    path.write_text(
        "\n".join(f"[2026-09-12T17:20:28.080Z] {json.dumps(entry)}" for entry in lines)
    )


def test_in_flight_tool_call_finds_unresolved_bash(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    share = tmp_path / ".local" / "share"
    _write_session_log(
        share,
        "s_abc",
        [
            {"content": [{"id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
            {"content": [{"tool_use_id": "t1", "type": "tool_result"}]},
            {
                "content": [
                    {
                        "id": "t2",
                        "name": "Bash",
                        "input": {"command": 'mkdir -p ~/Desktop/"Demo project"'},
                    }
                ]
            },
        ],
    )
    tool, command = sd.in_flight_tool_call("s_abc")
    assert tool == "Bash"
    assert command is not None and command.startswith("mkdir")


def test_in_flight_tool_call_none_when_all_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    share = tmp_path / ".local" / "share"
    _write_session_log(
        share,
        "s_done",
        [
            {"content": [{"id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
            {"content": [{"tool_use_id": "t1", "type": "tool_result"}]},
        ],
    )
    assert sd.in_flight_tool_call("s_done") == (None, None)


def test_in_flight_tool_call_missing_log(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert sd.in_flight_tool_call("s_missing") == (None, None)


def test_dialog_presenter_recency(monkeypatch):
    calls = {}

    class FakeCompleted:
        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(argv, **kwargs):
        calls.setdefault(argv[0], []).append(argv)
        if argv[0] == "pgrep":
            # Only UserNotificationCenter is running.
            return FakeCompleted(
                "123\n" if argv[-1] == "UserNotificationCenter" else ""
            )
        if argv[0] == "ps":
            return FakeCompleted("Fri Sep 12 17:20:19 2026\n")
        raise AssertionError(argv)

    monkeypatch.setattr(sd.subprocess, "run", fake_run)

    turn_start_before_dialog = dt.datetime(2026, 9, 12, 17, 20, 0)
    assert (
        sd.dialog_presenter_started_after(turn_start_before_dialog)
        == "UserNotificationCenter"
    )

    turn_start_after_dialog = dt.datetime(2026, 9, 12, 18, 0, 0)
    assert sd.dialog_presenter_started_after(turn_start_after_dialog) is None


def test_spoken_hint_blocked_names_command_and_dialog():
    diagnosis = sd.StallDiagnosis(
        elapsed_seconds=130,
        blocking_dialog_process="UserNotificationCenter",
        in_flight_tool="Bash",
        in_flight_command='mkdir -p ~/Desktop/"Demo project"',
    )
    hint = diagnosis.spoken_hint()
    assert "permission dialog" in hint
    assert "mkdir" in hint
    assert "2 minutes" in hint
    assert diagnosis.packet_payload()["event"] == "turn_stall_diagnosed"


def test_spoken_hint_soft_status_without_dialog():
    diagnosis = sd.StallDiagnosis(
        elapsed_seconds=200,
        blocking_dialog_process=None,
        in_flight_tool="Bash",
        in_flight_command="pytest -q",
    )
    hint = diagnosis.spoken_hint()
    assert "permission dialog" not in hint
    assert "still working" in hint


def test_speak_via_local_api_without_token(monkeypatch):
    # If the capability accessor fails (e.g. read-only data dir), the hint is
    # skipped rather than crashing the call.
    def _boom() -> str:
        raise OSError("no data dir")

    monkeypatch.setattr(sd, "get_local_api_token", _boom)
    assert sd.speak_via_local_api("hello") is False


def test_speak_via_local_api_mints_token_and_posts(monkeypatch):
    # A fresh install has no token file; the accessor mints one and the hint
    # authenticates against the local API. The original read-only lookup
    # returned False here (FT-9 follow-up 2026-09-13).
    monkeypatch.setattr(sd, "get_local_api_token", lambda: "x" * 40)
    captured: dict[str, object] = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.data.decode())
        return _Resp()

    monkeypatch.setattr(sd.urllib.request, "urlopen", _fake_urlopen)
    assert sd.speak_via_local_api("hello", agent_name="Dispatcher") is True
    assert captured["auth"] == "Bearer " + "x" * 40
    assert captured["url"].endswith("/api/user/say/")
    assert captured["body"] == {"agent_name": "Dispatcher", "text": "hello"}


def _make_state_db(tmp_path: Path, sessions: list[dict], turns: list[dict]) -> Path:
    import sqlite3

    db = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (id text primary key, name text, agent_name text, "
        "status text, active_turn_id text)"
    )
    conn.execute(
        "CREATE TABLE turns (id text primary key, session_id text, status text, "
        "last_error text, created_at text, updated_at text, finished_at text)"
    )
    for s in sessions:
        conn.execute(
            "INSERT INTO sessions VALUES (:id,:name,:agent_name,:status,:active_turn_id)",
            s,
        )
    for t in turns:
        conn.execute(
            "INSERT INTO turns VALUES "
            "(:id,:session_id,:status,:last_error,:created_at,:updated_at,:finished_at)",
            t,
        )
    conn.commit()
    conn.close()
    return db


def _subagent_db(tmp_path, *, last_error, ended, status="failed"):
    return _make_state_db(
        tmp_path,
        sessions=[
            {
                "id": "s_disp",
                "name": "dispatcher",
                "agent_name": "Dispatcher",
                "status": "completed",
                "active_turn_id": None,
            },
            {
                "id": "s_grace",
                "name": "list-documents-files",
                "agent_name": "Grace",
                "status": "failed",
                "active_turn_id": None,
            },
        ],
        turns=[
            {
                "id": "t_grace",
                "session_id": "s_grace",
                "status": status,
                "last_error": last_error,
                "created_at": ended,
                "updated_at": ended,
                "finished_at": ended,
            }
        ],
    )


def test_scan_blocked_turns_catches_spawned_subagent(tmp_path):
    """The key FT-9 case: a spawned sub-agent failed blocked-at-init."""
    now = dt.datetime(2026, 9, 12, 18, 6, 0)
    ended = (now - dt.timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S")
    db = _subagent_db(
        tmp_path, last_error="Control request timeout: initialize", ended=ended
    )
    blocked = sd.scan_blocked_turns(
        since=now - dt.timedelta(minutes=5), now=now, state_db_path=db
    )
    assert len(blocked) == 1
    b = blocked[0]
    assert b.agent_name == "Grace"
    hint = b.spoken_hint()
    assert "Grace" in hint
    assert "permission dialog" in hint
    assert b.packet_payload()["event"] == "agent_turn_blocked"


def test_scan_blocked_turns_ignores_other_errors(tmp_path):
    now = dt.datetime(2026, 9, 12, 18, 6, 0)
    ended = (now - dt.timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S")
    db = _subagent_db(tmp_path, last_error="Some unrelated network error", ended=ended)
    assert (
        sd.scan_blocked_turns(
            since=now - dt.timedelta(minutes=5), now=now, state_db_path=db
        )
        == []
    )


def test_scan_blocked_turns_respects_since_window(tmp_path):
    now = dt.datetime(2026, 9, 12, 18, 6, 0)
    ended = (now - dt.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    db = _subagent_db(
        tmp_path, last_error="Control request timeout: initialize", ended=ended
    )
    # A failure from before the watcher started is not surfaced.
    assert (
        sd.scan_blocked_turns(
            since=now - dt.timedelta(minutes=5), now=now, state_db_path=db
        )
        == []
    )


def test_scan_blocked_turns_missing_db_returns_empty(tmp_path):
    assert sd.scan_blocked_turns(state_db_path=tmp_path / "nope.sqlite3") == []
