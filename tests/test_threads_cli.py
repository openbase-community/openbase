"""``openbase-coder threads list`` / ``threads send`` with fakes for both backends."""

from __future__ import annotations

import importlib
import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from click.testing import CliRunner
from super_agents.claude_inbox import InboxDeliveryResult, InboxRecord

local_server = importlib.import_module("openbase_coder_cli.cli.local_server")
threads_cli = importlib.import_module("openbase_coder_cli.cli.threads")
terminal_cli = importlib.import_module("openbase_coder_cli.cli.threads_terminal")
delivery = importlib.import_module("openbase_coder_cli.cli.threads_delivery")
ts = importlib.import_module("openbase_coder_cli.terminal_sessions")

NOW = time.time()
CODEX_THREAD = "01a11d6a-d9c6-76e1-b9c8-43e450ba8581"
CLAUDE_SID = "9e42f768-814f-4360-8de1-2dd4e67a611a"
CLAUDE_THREAD = f"claude_{CLAUDE_SID.replace('-', '')}"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def codex_row(status: str = "idle") -> dict:
    return {
        "thread_id": CODEX_THREAD,
        "backend": "codex",
        "directory": "/work/app",
        "name": "Reply with pong",
        "status": status,
        "created_at": _iso(NOW - 120),
        "updated_at": _iso(NOW - 60),
        "backend_session_id": None,
    }


def claude_row() -> dict:
    return {
        "thread_id": CLAUDE_THREAD,
        "backend": "claude_code",
        "directory": "/work/app",
        "name": "tui-send-probe",
        "status": "completed",
        "created_at": _iso(NOW - 300),
        "updated_at": _iso(NOW - 30),
        "backend_session_id": CLAUDE_SID,
    }


def other_row() -> dict:
    return {
        "thread_id": "01a1aaaa-0000-7000-8000-000000000001",
        "backend": "codex",
        "directory": "/work/other",
        "name": "dispatched agent",
        "status": "running",
        "created_at": _iso(NOW - 500),
        "updated_at": _iso(NOW - 10),
        "backend_session_id": None,
    }


class FakeServer:
    """Answers the thread API the commands use and records the calls."""

    def __init__(self, rows: list[dict], *, detail: dict | None = None, pages: int = 1):
        self.rows = rows
        self.detail = detail
        self.pages = pages
        self.calls: list[tuple[str, str, dict | None]] = []
        self.turn_responses: list[tuple[int, dict]] = []

    def __call__(self, method, url, **kwargs):
        path = url.split("http://localhost:7999", 1)[1]
        self.calls.append((method, path, kwargs.get("json")))
        if method == "GET" and path.startswith("/api/threads/?"):
            page = int(path.split("page=")[1].split("&")[0]) if "page=" in path else 1
            payload = {
                "threads": self.rows if page == 1 else [],
                "next": f"/api/threads/?page={page + 1}&page_size=100&cursor=x"
                if page < self.pages
                else None,
            }
            return httpx.Response(200, json=payload)
        if method == "GET" and path == f"/api/threads/{CODEX_THREAD}/":
            return httpx.Response(200, json=self.detail or {})
        if method == "POST" and "/turns/" in path:
            if self.turn_responses:
                status, payload = self.turn_responses.pop(0)
                return httpx.Response(status, json=payload)
            return httpx.Response(201, json={"turn_id": "turn-1", "status": "started"})
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def fake_env(monkeypatch):
    """Wire the commands to fakes: server, processes, inbox records, state."""
    monkeypatch.setenv("OPENBASE_CODER_CLI_SERVER_URL", "http://localhost:7999")
    monkeypatch.setattr(local_server, "get_local_api_token", lambda: "local-token")
    record = InboxRecord(
        session_id=CLAUDE_SID, socket="/tmp/claude.sock", cwd="/work/app"
    )
    state = {
        "processes": [
            ts.TerminalProcess(
                pid=70108, started_at=NOW - 130, cwd="/work/app", attached=True
            )
        ],
        "terminals": [ts.ClaudeTerminal(record=record, live=True, busy=False)],
        "tracked": {},
        "delivered": [],
        "delivery_result": InboxDeliveryResult(written=True),
    }
    monkeypatch.setattr(
        terminal_cli, "live_codex_tui_processes", lambda *, now: state["processes"]
    )
    monkeypatch.setattr(terminal_cli, "claude_inbox_records", lambda: [record])
    monkeypatch.setattr(
        terminal_cli,
        "claude_terminals",
        lambda records, *, transcript_for: state["terminals"],
    )
    monkeypatch.setattr(terminal_cli, "tracked_thread_starts", lambda: state["tracked"])
    monkeypatch.setattr(delivery, "WAIT_POLL_SECONDS", 0)

    async def deliver(record, text, *, target_session_id, from_name):
        state["delivered"].append(
            (record.session_id, text, target_session_id, from_name)
        )
        return state["delivery_result"]

    monkeypatch.setattr(delivery, "deliver_steer", deliver)

    def install(server: FakeServer) -> FakeServer:
        monkeypatch.setattr(local_server.httpx, "request", server)
        return server

    state["install"] = install
    return state


def invoke(*args: str, input: str | None = None):
    return CliRunner().invoke(threads_cli.threads, list(args), input=input)


# --- list ----------------------------------------------------------------------------


def test_list_shows_terminal_sessions_only(fake_env):
    fake_env["install"](FakeServer([codex_row(), claude_row(), other_row()]))
    result = invoke("list")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split() == [
        "NAME",
        "BACKEND",
        "STATE",
        "STEERABLE",
        "FOLDER",
        "HOW",
    ]
    assert "Reply with pong" in result.output and "idle" in result.output
    assert "tui-send-probe" in result.output
    assert "dispatched agent" not in result.output


def test_list_all_appends_other_threads(fake_env):
    fake_env["install"](FakeServer([codex_row(), claude_row(), other_row()]))
    result = invoke("list", "--all")
    assert result.exit_code == 0, result.output
    assert "dispatched agent" in result.output
    assert "via Openbase" in result.output


def test_list_json_reports_steerability(fake_env):
    fake_env["processes"] = [
        ts.TerminalProcess(pid=1, started_at=NOW - 5, cwd="/work/fresh", attached=True)
    ]
    fake_env["install"](FakeServer([claude_row()]))
    result = invoke("list", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    by_name = {item["name"]: item for item in payload["sessions"]}
    assert by_name["tui-send-probe"]["steerable"] is True
    assert by_name["tui-send-probe"]["thread_id"] == CLAUDE_THREAD
    assert by_name["codex (no messages yet)"]["steerable"] is False
    assert by_name["codex (no messages yet)"]["pid"] == 1


def test_list_without_server_falls_back_to_local_facts(fake_env, monkeypatch):
    def down(method, url, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(local_server.httpx, "request", down)
    result = invoke("list")
    assert result.exit_code == 0, result.output
    assert "Unable to reach the local Openbase Coder server" in result.output
    assert f"claude {CLAUDE_SID[:8]}" in result.output


def test_list_reports_nothing_open(fake_env):
    fake_env["processes"] = []
    fake_env["terminals"] = []
    fake_env["install"](FakeServer([other_row()]))
    result = invoke("list")
    assert result.exit_code == 0, result.output
    assert "No terminal sessions" in result.output


def test_fetch_threads_follows_next_until_enough(fake_env):
    server = fake_env["install"](FakeServer([codex_row()], pages=4))
    rows = terminal_cli.fetch_threads(enough=lambda rows: len(rows) >= 1)
    assert len(rows) == 1
    assert len(server.calls) == 1
    server.calls.clear()
    terminal_cli.fetch_threads(pages=3)
    assert [call[1] for call in server.calls] == [
        "/api/threads/?page_size=100",
        "/api/threads/?page=2&page_size=100&cursor=x",
        "/api/threads/?page=3&page_size=100&cursor=x",
    ]


# --- send ---------------------------------------------------------------------------------


def test_send_to_claude_terminal_uses_its_inbox(fake_env):
    fake_env["install"](FakeServer([codex_row(), claude_row()]))
    result = invoke("send", "tui-send-probe", "please stop and summarize")
    assert result.exit_code == 0, result.output
    assert fake_env["delivered"] == [
        (CLAUDE_SID, "please stop and summarize", CLAUDE_SID, "openbase-coder")
    ]
    assert "submitted to tui-send-probe via its Claude Code inbox; delivery is unconfirmed" in result.output
    assert "crossSessionInbound" in result.output


def test_send_resolves_a_claude_session_id_prefix_without_a_thread_row(fake_env):
    fake_env["install"](FakeServer([codex_row()]))
    result = invoke("send", CLAUDE_SID[:8], "hello")
    assert result.exit_code == 0, result.output
    assert fake_env["delivered"][0][1] == "hello"


def test_send_reports_inbox_failures(fake_env):
    fake_env["delivery_result"] = InboxDeliveryResult(
        written=False, reason="socket_unreachable"
    )
    fake_env["install"](FakeServer([claude_row()]))
    result = invoke("send", "tui-send-probe", "hello")
    assert result.exit_code == 1
    assert "no longer accepts connections" in result.output


def test_send_to_idle_codex_starts_a_turn(fake_env):
    server = fake_env["install"](FakeServer([codex_row("idle"), claude_row()]))
    result = invoke("send", "pong", "fix the build")
    assert result.exit_code == 0, result.output
    posts = [call for call in server.calls if call[0] == "POST"]
    assert posts == [
        ("POST", f"/api/threads/{CODEX_THREAD}/turns/", {"prompt": "fix the build"})
    ]
    assert "sent to Reply with pong via Openbase" in result.output


def test_send_to_busy_codex_steers(fake_env):
    server = fake_env["install"](FakeServer([codex_row("running")]))
    result = invoke("send", CODEX_THREAD, "use pnpm instead")
    assert result.exit_code == 0, result.output
    posts = [call for call in server.calls if call[0] == "POST"]
    assert posts == [
        (
            "POST",
            f"/api/threads/{CODEX_THREAD}/turns/steer/",
            {"prompt": "use pnpm instead"},
        )
    ]
    assert "steered Reply with pong" in result.output


def test_send_retries_the_other_path_when_the_busy_guess_raced(fake_env):
    server = fake_env["install"](FakeServer([codex_row("idle")]))
    server.turn_responses = [
        (
            400,
            {
                "error": f"Thread {CODEX_THREAD} already has an active turn. Interrupt it first."
            },
        ),
        (200, {"turn_id": "turn-2", "steered": True}),
    ]
    result = invoke("send", "pong", "hurry")
    assert result.exit_code == 0, result.output
    assert [call[1] for call in server.calls if call[0] == "POST"] == [
        f"/api/threads/{CODEX_THREAD}/turns/",
        f"/api/threads/{CODEX_THREAD}/turns/steer/",
    ]
    assert "steered Reply with pong" in result.output


def test_send_surfaces_other_server_errors(fake_env):
    server = fake_env["install"](FakeServer([codex_row("idle")]))
    server.turn_responses = [
        (400, {"error": "Thread is read-only here: it moved to the hub."})
    ]
    result = invoke("send", "pong", "hurry")
    assert result.exit_code == 1
    assert "read-only here" in result.output
    assert len([call for call in server.calls if call[0] == "POST"]) == 1


def test_send_to_a_thread_not_open_in_a_terminal_goes_through_openbase(fake_env):
    server = fake_env["install"](FakeServer([codex_row(), other_row()]))
    result = invoke("send", "dispatched agent", "status?")
    assert result.exit_code == 0, result.output
    assert [call[1] for call in server.calls if call[0] == "POST"] == [
        "/api/threads/01a1aaaa-0000-7000-8000-000000000001/turns/steer/"
    ]


def test_send_refuses_a_session_that_cannot_be_reached_yet(fake_env):
    fake_env["processes"] = [
        ts.TerminalProcess(pid=1, started_at=NOW - 5, cwd="/work/fresh", attached=True)
    ]
    fake_env["install"](FakeServer([]))
    result = invoke("send", "codex (no messages yet)", "hello")
    assert result.exit_code == 1
    assert "cannot be messaged yet" in result.output


def test_send_rejects_unknown_and_ambiguous_names(fake_env):
    rows = [codex_row(), claude_row(), {**other_row(), "name": "pong two"}]
    fake_env["install"](FakeServer(rows))
    unknown = invoke("send", "nothing-like-this", "hi")
    assert unknown.exit_code == 1
    assert "No session or thread matches" in unknown.output
    # Terminal sessions win over other threads, so "pong" is unique...
    assert invoke("send", "pong", "hi").exit_code == 0
    # ...while a substring shared by two non-terminal threads is not.
    rows.append({**other_row(), "thread_id": "x-2", "name": "pong three"})
    ambiguous = invoke("send", "pong t", "hi")
    assert ambiguous.exit_code == 1
    assert "matches more than one" in ambiguous.output


def test_send_reads_the_message_from_stdin(fake_env):
    server = fake_env["install"](FakeServer([codex_row()]))
    result = invoke("send", "pong", input="line one\nline two\n")
    assert result.exit_code == 0, result.output
    posts = [call for call in server.calls if call[0] == "POST"]
    assert posts[0][2] == {"prompt": "line one\nline two"}
    empty = invoke("send", "pong", input="  \n")
    assert empty.exit_code == 2
    assert "message is empty" in empty.output


def test_send_wait_prints_the_reply_once_the_turn_completes(fake_env):
    now = time.time()
    detail_running = {
        "current_turn": {"turn_id": "turn-9", "started_at": _iso(NOW - 20)},
        "turn_history": [],
    }
    detail_done = {
        "current_turn": None,
        "turn_history": [
            {
                "turn_id": "turn-8",
                "started_at": _iso(NOW - 900),
                "completed_at": _iso(NOW - 800),
                "status": "completed",
                "accumulated_output": "old reply",
            },
            {
                "turn_id": "turn-9",
                "started_at": _iso(now - 20),
                "completed_at": _iso(now + 5),
                "status": "completed",
                "accumulated_output": "steered",
            },
        ],
    }
    server = fake_env["install"](
        FakeServer([codex_row("running")], detail=detail_running)
    )
    polls = {"n": 0}
    original = server.__call__

    def respond(method, url, **kwargs):
        if method == "GET" and url.endswith(f"/api/threads/{CODEX_THREAD}/"):
            polls["n"] += 1
            if polls["n"] >= 3:
                server.detail = detail_done
        return original(method, url, **kwargs)

    fake_env["install"](respond)
    result = invoke("send", "pong", "finish up", "--wait", "--timeout", "30")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "steered"
    assert polls["n"] == 3


def test_send_wait_times_out(fake_env, monkeypatch):
    detail = {"current_turn": {"turn_id": "t"}, "turn_history": []}
    fake_env["install"](FakeServer([codex_row("running")], detail=detail))
    clock = {"t": 0.0}
    monkeypatch.setattr(
        delivery.time,
        "monotonic",
        lambda: clock.__setitem__("t", clock["t"] + 5) or clock["t"],
    )
    result = invoke("send", "pong", "finish up", "--wait", "--timeout", "12")
    assert result.exit_code == 1
    assert "No reply within 12s" in result.output


def test_send_wait_json_includes_the_reply(fake_env):
    now = time.time()
    detail = {
        "current_turn": None,
        "turn_history": [
            {
                "turn_id": "turn-1",
                "started_at": _iso(now),
                "completed_at": _iso(now + 3),
                "status": "completed",
                "accumulated_output": "pong2",
            }
        ],
    }
    fake_env["install"](FakeServer([codex_row()], detail=detail))
    result = invoke("send", "pong", "ping", "--wait", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["reply"]["accumulated_output"] == "pong2"
    assert payload["delivery"] == "start"


def test_send_wait_needs_a_thread_openbase_lists(fake_env):
    fake_env["install"](FakeServer([]))
    result = invoke("send", CLAUDE_SID[:8], "hello", "--wait")
    assert result.exit_code == 1
    assert "Cannot wait" in result.output
    assert fake_env["delivered"]


def test_server_inbox_receipt_is_preserved_without_retry(fake_env):
    server = fake_env["install"](FakeServer([codex_row("running")]))
    receipt = {"delivery": "inbox", "confirmed": False, "steered": True,
               "turnId": None, "startedImmediately": False, "messageId": "frame-1"}
    server.turn_responses = [(200, receipt)]
    result = invoke("send", CODEX_THREAD, "follow up")
    assert result.exit_code == 0, result.output
    assert "delivery is unconfirmed" in result.output
    assert "Message steered" not in result.output
    assert len([call for call in server.calls if call[0] == "POST"]) == 1


def test_ambiguous_inbox_write_does_not_retry_or_claim_failure(fake_env):
    fake_env["delivery_result"] = InboxDeliveryResult(
        written=False, reason="write_failed", may_have_been_written=True, message_id="frame-2",
    )
    server = fake_env["install"](FakeServer([claude_row()]))
    result = invoke("send", "tui-send-probe", "follow up")
    assert result.exit_code == 0, result.output
    assert "delivery is unconfirmed" in result.output
    assert "Do not blindly retry" in result.output
    assert len(fake_env["delivered"]) == 1
    assert not [call for call in server.calls if call[0] == "POST"]


def test_unconfirmed_wait_cannot_misattribute_an_old_completion(fake_env):
    server = fake_env["install"](FakeServer([claude_row()]))
    result = invoke("send", "tui-send-probe", "follow up", "--wait")
    assert result.exit_code == 1
    assert "Cannot wait for confirmed work" in result.output
    assert len(fake_env["delivered"]) == 1
    assert not [call for call in server.calls if call[1] == f"/api/threads/{CLAUDE_THREAD}/"]


def test_unavailable_receipt_does_not_claim_delivery(fake_env):
    server = fake_env["install"](FakeServer([codex_row("running")]))
    server.turn_responses = [(200, {"delivery": "unavailable", "confirmed": False,
                                  "steered": False, "turnId": None})]
    result = invoke("send", CODEX_THREAD, "follow up")
    assert result.exit_code == 0, result.output
    assert "Nothing was delivered or queued" in result.output
    assert "Message delivered" not in result.output
    assert len([call for call in server.calls if call[0] == "POST"]) == 1
