from __future__ import annotations

import json
from pathlib import Path

from super_agents.claude_inbox import InboxRecord

from openbase_coder_cli import terminal_sessions as ts

NOW = 1_800_000_000.0
CODEX_BIN = "/opt/node_modules/@openai/codex-darwin-arm64/vendor/bin/codex"


def _iso(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(
    thread_id: str,
    *,
    backend: str = "codex",
    directory: str = "/work/app",
    name: str | None = "fix the build",
    status: str = "idle",
    created: float = NOW - 60,
    updated: float | None = None,
    session_id: str | None = None,
) -> dict:
    return {
        "thread_id": thread_id,
        "backend": backend,
        "directory": directory,
        "name": name,
        "status": status,
        "created_at": _iso(created),
        "updated_at": _iso(updated if updated is not None else created),
        "backend_session_id": session_id,
    }


# --- ps / lsof parsing -----------------------------------------------------


def test_parse_ps_elapsed_handles_every_etime_shape():
    assert ts.parse_ps_elapsed("05:07") == 307
    assert ts.parse_ps_elapsed("01:02:03") == 3723
    assert ts.parse_ps_elapsed("2-01:00:00") == 2 * 86400 + 3600


def test_codex_tui_processes_keeps_tuis_and_resolves_cwd():
    ps = "\n".join(
        [
            f" 101 00:30 {CODEX_BIN} -p openbase --remote unix:// -C /work/app",
            f" 102 02:00:00 {CODEX_BIN} --yolo",
            f" 103 00:10 {CODEX_BIN} resume --remote unix:// 01a1d6aa-d9c6-76e1-b9c8-43e450ba8581",
            f" 104 00:05 {CODEX_BIN} app-server --listen unix://",
            f" 105 00:05 {CODEX_BIN} exec 'summarize'",
            " 106 00:05 node /opt/bin/codex --yolo",
            f" 107 00:05 {CODEX_BIN} -m gpt-5.5 fix the build",
        ]
    )
    lsof = "p102\nfcwd\nn/work/other\np107\nfcwd\nn/work/prompted\n"
    processes = ts.codex_tui_processes(ps, lsof, now=NOW)
    by_pid = {process.pid: process for process in processes}
    assert set(by_pid) == {101, 102, 103, 107}
    assert by_pid[101].cwd == "/work/app"
    assert by_pid[101].attached is True
    assert by_pid[101].started_at == NOW - 30
    assert by_pid[102].cwd == "/work/other"
    assert by_pid[102].attached is False
    assert by_pid[103].thread_id == "01a1d6aa-d9c6-76e1-b9c8-43e450ba8581"
    assert by_pid[107].cwd == "/work/prompted"
    assert by_pid[107].thread_id is None


def test_parse_lsof_cwds_ignores_malformed_lines():
    assert ts.parse_lsof_cwds("pabc\nn/x\np7\nfcwd\nn/home\n") == {7: "/home"}


# --- Claude transcripts ---------------------------------------------------------


def _write_transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    return path


def test_claude_transcript_busy_reads_the_last_message(tmp_path):
    idle = _write_transcript(
        tmp_path / "idle.jsonl",
        [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "message": {"stop_reason": "end_turn"}},
            {"type": "attachment"},
        ],
    )
    tool = _write_transcript(
        tmp_path / "tool.jsonl",
        [{"type": "assistant", "message": {"stop_reason": "tool_use"}}],
    )
    prompt = _write_transcript(
        tmp_path / "prompt.jsonl",
        [
            {"type": "assistant", "message": {"stop_reason": "end_turn"}},
            {"type": "user", "message": {"role": "user", "content": "go"}},
        ],
    )
    assert ts.claude_transcript_busy(idle) is False
    assert ts.claude_transcript_busy(tool) is True
    assert ts.claude_transcript_busy(prompt) is True
    assert ts.claude_transcript_busy(tmp_path / "empty.jsonl") is None
    (tmp_path / "meta.jsonl").write_text('{"type":"summary"}\nnot json\n')
    assert ts.claude_transcript_busy(tmp_path / "meta.jsonl") is None


def test_claude_inbox_records_reads_every_registry_file(tmp_path):
    registry = tmp_path / "inbox-registry"
    registry.mkdir()
    (registry / "sid-1.json").write_text(
        json.dumps({"sessionId": "sid-1", "socket": "/tmp/1.sock", "cwd": "/w"})
    )
    (registry / "broken.json").write_text("{nope")
    (registry / "no-socket.json").write_text(json.dumps({"sessionId": "x"}))
    records = ts.claude_inbox_records(registry)
    assert [record.session_id for record in records] == ["sid-1"]


def test_claude_terminals_probe_sockets_and_transcripts(tmp_path):
    live_socket = tmp_path / "live.sock"
    live_socket.write_text("")
    dead_socket = tmp_path / "dead.sock"
    dead_socket.write_text("")
    transcript = _write_transcript(
        tmp_path / "t.jsonl",
        [{"type": "assistant", "message": {"stop_reason": "end_turn"}}],
    )
    records = [
        InboxRecord(session_id="live", socket=str(live_socket), cwd="/w"),
        InboxRecord(session_id="dead", socket=str(dead_socket), cwd="/w"),
        InboxRecord(session_id="gone", socket=str(tmp_path / "missing.sock"), cwd="/w"),
    ]
    probed: list[Path] = []

    def accepts(path: Path) -> bool:
        probed.append(path)
        return path == live_socket

    terminals = ts.claude_terminals(
        records,
        socket_accepts=accepts,
        transcript_for=lambda record: (
            transcript if record.session_id == "live" else None
        ),
    )
    assert [(t.record.session_id, t.live, t.busy) for t in terminals] == [
        ("live", True, False),
        ("dead", False, None),
        ("gone", False, None),
    ]
    # A missing socket file is never probed.
    assert probed == [live_socket, dead_socket]


# --- joining ------------------------------------------------------------------------


def _claude_terminal(session_id: str, *, live: bool = True, busy: bool | None = False):
    return ts.ClaudeTerminal(
        record=InboxRecord(
            session_id=session_id, socket=f"/tmp/{session_id}.sock", cwd="/work/app"
        ),
        live=live,
        busy=busy,
    )


def test_terminal_sessions_lists_live_claude_sessions_with_thread_names():
    threads = [
        _row(
            "claude_aaa",
            backend="claude_code",
            name="tui-send",
            session_id="aaa",
            status="completed",
        ),
    ]
    sessions = ts.terminal_sessions(
        threads,
        codex_processes=[],
        claude_terminals=[
            _claude_terminal("aaa", busy=True),
            _claude_terminal("bbb", busy=None),
            _claude_terminal("ccc", live=False),
        ],
        tracked_starts={},
    )
    assert [(s.name, s.thread_id, s.state, s.steerable) for s in sessions] == [
        ("claude bbb", None, "unknown", True),
        ("tui-send", "claude_aaa", "busy", True),
    ]
    assert "once a message is typed" in sessions[0].reason
    assert sessions[1].backend_session_id == "aaa"


def test_terminal_sessions_matches_codex_processes_to_threads():
    threads = [
        _row("t-old", created=NOW - 3600, updated=NOW - 3600),
        _row("t-new", name="Reply with pong", created=NOW - 50, status="running"),
        _row("t-agent", name="dispatched", created=NOW - 40),
        _row("t-elsewhere", directory="/work/other", created=NOW - 50),
    ]
    processes = [
        ts.TerminalProcess(pid=1, started_at=NOW - 55, cwd="/work/app", attached=True)
    ]
    sessions = ts.terminal_sessions(
        threads,
        codex_processes=processes,
        claude_terminals=[],
        # Dispatched: tracked from its creation. The terminal's own thread is
        # tracked too, but only since a `threads send` minutes later.
        tracked_starts={"t-agent": NOW - 39, "t-new": NOW - 10},
    )
    assert [(s.name, s.thread_id, s.state, s.pid) for s in sessions] == [
        ("Reply with pong", "t-new", "busy", 1)
    ]


def test_terminal_sessions_prefers_newest_thread_and_resumed_ids():
    threads = [
        _row("t-first", name="first", created=NOW - 50),
        _row("t-second", name="second", created=NOW - 20),
        _row("t-resumed", name="old one", created=NOW - 9000, updated=NOW - 9000),
    ]
    processes = [
        ts.TerminalProcess(pid=1, started_at=NOW - 55, cwd="/work/app"),
        ts.TerminalProcess(
            pid=2, started_at=NOW - 10, cwd="/work/app", thread_id="t-resumed"
        ),
    ]
    sessions = ts.terminal_sessions(
        threads,
        codex_processes=processes,
        claude_terminals=[],
        tracked_starts={},
    )
    assert sorted((s.thread_id, s.pid) for s in sessions) == [
        ("t-resumed", 2),
        ("t-second", 1),
    ]


def test_terminal_sessions_uses_recent_activity_for_resumed_threads():
    threads = [_row("t-old", name="old", created=NOW - 9000, updated=NOW - 5)]
    processes = [ts.TerminalProcess(pid=1, started_at=NOW - 30, cwd="/work/app")]
    sessions = ts.terminal_sessions(
        threads,
        codex_processes=processes,
        claude_terminals=[],
        tracked_starts={},
    )
    assert [s.thread_id for s in sessions] == ["t-old"]


def test_terminal_sessions_reports_a_tui_without_a_thread_yet():
    processes = [
        ts.TerminalProcess(pid=9, started_at=NOW - 5, cwd="/work/fresh", attached=True)
    ]
    sessions = ts.terminal_sessions(
        [], codex_processes=processes, claude_terminals=[], tracked_starts={}
    )
    assert len(sessions) == 1
    assert sessions[0].steerable is False
    assert sessions[0].thread_id is None
    assert "first message" in sessions[0].reason
    assert sessions[0].to_json()["state"] == "unknown"


def test_claude_transcript_busy_skips_local_slash_commands(tmp_path):
    transcript = _write_transcript(
        tmp_path / "local.jsonl",
        [
            {"type": "assistant", "message": {"stop_reason": "end_turn"}},
            {
                "type": "user",
                "isMeta": True,
                "message": {"content": "<local-command-caveat>x"},
            },
            {
                "type": "user",
                "message": {"content": "<command-name>/model</command-name>"},
            },
            {
                "type": "user",
                "message": {
                    "content": [{"type": "text", "text": "<local-command-stdout>ok"}]
                },
            },
            {"type": "system", "subtype": "turn_duration"},
        ],
    )
    assert ts.claude_transcript_busy(transcript) is False
    tool = _write_transcript(
        tmp_path / "tool-result.jsonl",
        [{"type": "user", "message": {"content": [{"type": "tool_result"}]}}],
    )
    assert ts.claude_transcript_busy(tool) is True


def test_unlisted_claude_session_uses_its_own_title(tmp_path):
    live_socket = tmp_path / "live.sock"
    live_socket.write_text("")
    transcript = _write_transcript(
        tmp_path / "t.jsonl",
        [{"type": "custom-title", "customTitle": "tui-send-probe"}],
    )
    terminals = ts.claude_terminals(
        [InboxRecord(session_id="ddd", socket=str(live_socket), cwd="/work/app")],
        socket_accepts=lambda path: True,
        transcript_for=lambda record: transcript,
    )
    assert terminals[0].title == "tui-send-probe"
    sessions = ts.terminal_sessions(
        [], codex_processes=[], claude_terminals=terminals, tracked_starts={}
    )
    assert [s.name for s in sessions] == ["tui-send-probe"]
    assert "once a message is typed" in sessions[0].reason


def test_claude_transcript_busy_widens_past_large_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "_TRANSCRIPT_TAIL_BYTES", 256)
    filler = [{"type": "attachment", "blob": "x" * 200} for _ in range(20)]
    transcript = _write_transcript(
        tmp_path / "big.jsonl",
        [{"type": "assistant", "message": {"stop_reason": "end_turn"}}, *filler],
    )
    assert ts.claude_transcript_busy(transcript) is False
