"""Persistent pty sessions for agent-driven interactive logins."""

from __future__ import annotations

import importlib
import os
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


def test_start_notifies_the_agents_own_thread_by_default(monkeypatch):
    runner = CliRunner()
    seen = []
    monkeypatch.setattr(
        pty_session,
        "start",
        lambda name, command, notify_thread=None: (
            seen.append(notify_thread) or {"running": True, "exit_code": None}
        ),
    )
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setenv("SUPER_AGENTS_THREAD_ID", "s_abc")
    result = runner.invoke(pty_cli.pty, ["start", "a", "--", "true"])
    assert result.exit_code == 0, result.output
    assert "follow-up turn" in result.output
    runner.invoke(pty_cli.pty, ["start", "b", "--no-notify", "--", "true"])
    monkeypatch.delenv("SUPER_AGENTS_THREAD_ID")
    monkeypatch.setenv("CODEX_THREAD_ID", "019f-uuid")
    runner.invoke(pty_cli.pty, ["start", "c", "--", "true"])
    monkeypatch.delenv("CODEX_THREAD_ID")
    runner.invoke(pty_cli.pty, ["start", "d", "--", "true"])
    assert seen == ["s_abc", None, "019f-uuid", None]


def test_logins_open_on_the_phone_on_any_host(monkeypatch):
    import shutil as shutil_module

    monkeypatch.setattr(shutil_module, "which", lambda name: None)
    shim = pty_session.phone_browser_command()
    text = open(shim).read()
    assert "-m openbase_coder_cli browser open" in text
    assert os.access(shim, os.X_OK)
    pty_session.start("browser", ["sh", "-c", 'echo "BROWSER=$BROWSER"'])
    output, _ = _read_until("browser", "BROWSER=")
    assert f"BROWSER={shim}" in output


def test_login_sessions_do_not_inherit_the_agents_gateway_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "obmt_gateway")
    monkeypatch.setenv(
        "ANTHROPIC_BASE_URL", "https://app.example/api/openbase/llm/anthropic"
    )
    pty_session.start(
        "env",
        [
            "sh",
            "-c",
            'echo "token=${ANTHROPIC_AUTH_TOKEN:-none} base=${ANTHROPIC_BASE_URL:-none}"',
        ],
    )
    output, _ = _read_until("env", "base=")
    assert "token=none base=none" in output


def test_unfinished_ai_login_restores_the_previous_login(monkeypatch, tmp_path):
    from openbase_coder_cli import ai_account

    assert pty_session.login_provider_for(["/usr/bin/codex", "login"]) == "codex"
    assert pty_session.login_provider_for(["codex", "login", "status"]) is None
    assert (
        pty_session.login_provider_for(["claude", "auth", "login", "--claudeai"])
        == "claude_code"
    )
    assert pty_session.login_provider_for(["gcloud", "auth", "login"]) is None

    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens": "old"}')
    monkeypatch.setattr(ai_account, "_credential_paths", lambda provider: [auth])
    codex = tmp_path / "codex"
    codex.write_text(f"#!/bin/sh\nrm -f {auth}\nexit 1\n")
    codex.chmod(0o755)
    paths = pty_session.session_paths("relink")
    paths.root.mkdir(parents=True, mode=0o700)
    os.mkfifo(paths.input, 0o600)
    paths.output.touch(mode=0o600)
    pty_session._write_meta(
        paths, {"command": [str(codex), "login"], "exit_code": None}
    )
    pty_session._hold("relink")
    assert auth.read_text() == '{"tokens": "old"}'
