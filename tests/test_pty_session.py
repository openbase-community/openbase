"""Persistent pty sessions for agent-driven interactive logins."""

from __future__ import annotations

import importlib
import socket
import time

import pytest
from click.testing import CliRunner

from openbase_coder_cli import pty_session

# `openbase_coder_cli.cli` re-exports the click groups under the module names.
ports_cli = importlib.import_module("openbase_coder_cli.cli.ports")
pty_cli = importlib.import_module("openbase_coder_cli.cli.pty")


@pytest.fixture(autouse=True)
def state_dir(monkeypatch, tmp_path):
    # The holder is a separate process: point it at the same state dir.
    data_dir = tmp_path / ".openbase"
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(data_dir))
    monkeypatch.setattr(pty_session, "STATE_DIR", data_dir / "pty")
    yield
    for item in pty_session.list_sessions():
        pty_session.stop(item["name"])


def _read_until(name, needle, timeout=5.0):
    collected = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = pty_session.read(name, wait=0.5)
        collected += result["output"]
        if needle in collected or not result["running"]:
            return collected, result
    raise AssertionError(f"{needle!r} not seen; got {collected!r}")


def test_prompt_reply_and_secret_redaction():
    pty_session.start(
        "login",
        ["sh", "-c", 'printf "Enter authorization code: "; read c; echo "got [$c]"'],
    )
    output, _ = _read_until("login", "authorization code")
    assert "Enter authorization code:" in output
    pty_session.send("login", "TOP-SECRET-42", secret=True)
    output, result = _read_until("login", "got")
    assert "TOP-SECRET-42" not in output
    assert "got [[secret]]" in output
    log = pty_session.session_paths("login").output
    assert "TOP-SECRET-42" not in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600
    _read_until("login", "\0", timeout=3)
    status = pty_session.session_status("login")
    assert status["running"] is False and status["exit_code"] == 0


def test_read_is_incremental_and_all_replays():
    pty_session.start("echo", ["sh", "-c", "echo one; sleep 0.3; echo two; sleep 5"])
    first, _ = _read_until("echo", "one")
    second, _ = _read_until("echo", "two")
    assert "one" not in second
    assert "one" in pty_session.read("echo", everything=True)["output"]
    pty_session.stop("echo")


def test_names_and_lifecycle_errors():
    with pytest.raises(pty_session.PtySessionError):
        pty_session.session_paths("../evil")
    with pytest.raises(pty_session.PtySessionError):
        pty_session.read("missing")
    pty_session.start("done", ["true"])
    _read_until("done", "\0", timeout=3)
    with pytest.raises(pty_session.PtySessionError, match="not running"):
        pty_session.send("done", "x")
    with pytest.raises(pty_session.PtySessionError, match="one line"):
        pty_session.start("busy", ["sleep", "5"])
        pty_session.send("busy", "a\nb")
    with pytest.raises(pty_session.PtySessionError, match="still running"):
        pty_session.start("busy", ["sleep", "5"])
    # A finished session's name can be reused.
    pty_session.start("done", ["true"])


def test_cli_round_trip_keeps_secrets_out_of_argv():
    runner = CliRunner()
    result = runner.invoke(
        pty_cli.pty, ["start", "cli", "--", "sh", "-c", "read c; echo len=${#c}"]
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(pty_cli.pty, ["send", "cli", "--secret"], input="abcdef\n")
    assert result.exit_code == 0, result.output
    assert "abcdef" not in result.output
    output, _ = _read_until("cli", "len=")
    assert "len=6" in output
    status = runner.invoke(pty_cli.pty, ["status"])
    assert "cli" in status.output


def test_clean_output_strips_escapes_and_redraws():
    assert pty_session.clean_output("\x1b[1mbold\x1b[0m\r\nnext") == "bold\nnext"
    assert pty_session.clean_output("50%\r100%") == "100%"


def test_ports_listening_sees_a_new_loopback_server():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    try:
        rows = ports_cli.listening_ports()
        assert any(row["port"] == port for row in rows)
        result = CliRunner().invoke(ports_cli.ports, ["listening", "--json"])
        assert str(port) in result.output
    finally:
        server.close()


def test_ended_session_queues_a_follow_up_turn(monkeypatch):
    import os

    with pytest.raises(pty_session.PtySessionError, match="thread id"):
        pty_session.start("bad", ["true"], notify_thread="a/b")
    prompt = pty_session.notify_prompt("gcloud", 0, "exited")
    assert "pty read gcloud" in prompt and "status command" in prompt

    queued = []
    monkeypatch.setattr(
        pty_session, "_notify_thread", lambda t, p: queued.append((t, p))
    )
    # Run the holder in-process so the patched notifier is the one called.
    paths = pty_session.session_paths("inproc")
    paths.root.mkdir(parents=True, mode=0o700)
    os.mkfifo(paths.input, 0o600)
    paths.output.touch(mode=0o600)
    pty_session._write_meta(
        paths, {"command": ["true"], "exit_code": None, "notify_thread": "s_123"}
    )
    pty_session._hold("inproc")
    assert queued and queued[0][0] == "s_123"
    assert "inproc ended: exited, exit 0" in queued[0][1]
