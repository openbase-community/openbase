from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from asgiref.testing import ApplicationCommunicator  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    consumers,  # noqa: E402
    thread_terminal,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_terminal import (  # noqa: E402
    TerminalLaunch,
    TerminalRegistry,
    TerminalSession,
    TerminalUnavailableError,
    resolve_terminal_launch,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY only")


def _binaries(name: str) -> Path:
    return Path(f"/opt/bin/{name}")


def _resolve(**overrides):
    kwargs = {
        "thread_id": "thread-1",
        "backend": "codex",
        "backend_session_id": None,
        "directory": None,
        "find_binary": _binaries,
        "codex_endpoint": lambda: "unix://",
        "claude_env": lambda backend: (
            {"ANTHROPIC_BASE_URL": f"cloud-{backend}"}
            if backend == "openbase_cloud"
            else {}
        ),
    }
    kwargs.update(overrides)
    return resolve_terminal_launch(**kwargs)


def test_codex_thread_attaches_to_the_managed_app_server(tmp_path):
    launch = _resolve(directory=str(tmp_path))

    assert launch.argv == [
        "/opt/bin/codex",
        "resume",
        "thread-1",
        "--remote",
        "unix://",
    ]
    assert launch.cwd == str(tmp_path)
    assert launch.target == "codex"
    assert launch.env["TERM"] == "xterm-256color"


def test_launch_env_drops_inherited_agent_session_markers(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    monkeypatch.setenv("CODEX_THREAD_ID", "parent")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/cfg")

    launch = _resolve(backend="claude_code", backend_session_id="sess-9")

    assert "CLAUDECODE" not in launch.env
    assert "CLAUDE_CODE_CHILD_SESSION" not in launch.env
    assert "CODEX_THREAD_ID" not in launch.env
    assert launch.env["CLAUDE_CONFIG_DIR"] == "/cfg"


def test_cloud_codex_thread_uses_codex_cli():
    launch = _resolve(backend="openbase_cloud_codex")

    assert launch.argv[:3] == ["/opt/bin/codex", "resume", "thread-1"]
    assert launch.target == "codex"


def test_claude_thread_resumes_backend_session():
    launch = _resolve(backend="claude_code", backend_session_id="sess-9")

    assert launch.argv == ["/opt/bin/claude", "--resume", "sess-9"]
    assert launch.target == "claude_code"
    assert "ANTHROPIC_BASE_URL" not in launch.env or not launch.env[
        "ANTHROPIC_BASE_URL"
    ].startswith("cloud-")


def test_cloud_claude_thread_gets_cloud_env():
    launch = _resolve(backend="openbase_cloud", backend_session_id="sess-9")

    assert launch.env["ANTHROPIC_BASE_URL"] == "cloud-openbase_cloud"


def test_legacy_payload_without_backend_infers_from_session_id():
    assert _resolve(backend=None).target == "codex"
    assert _resolve(backend=None, backend_session_id="sess-1").target == "claude_code"


def test_claude_thread_without_session_is_unavailable():
    with pytest.raises(TerminalUnavailableError, match="no saved session"):
        _resolve(backend="claude_code")


def test_missing_binary_is_unavailable():
    with pytest.raises(TerminalUnavailableError, match="Codex CLI"):
        _resolve(find_binary=lambda name: None)


def test_unknown_backend_is_unavailable():
    with pytest.raises(TerminalUnavailableError, match="mystery"):
        _resolve(backend="mystery")


def test_missing_directory_falls_back_to_home():
    launch = _resolve(directory="/definitely/not/here")

    assert launch.cwd == str(Path.home())


def _shell_launch(script: str) -> TerminalLaunch:
    return TerminalLaunch(
        backend="codex",
        argv=["/bin/sh", "-c", script],
        cwd=str(Path.home()),
        env={**os.environ, "TERM": "xterm-256color"},
    )


async def _collect_until(events: list, predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out; events={events!r}")
        await asyncio.sleep(0.02)


def _output(events: list) -> bytes:
    return b"".join(payload for kind, payload in events if kind == "output")


async def test_session_runs_in_a_sized_tty_and_echoes_input():
    session = TerminalSession(
        "t1", _shell_launch("stty size; read line; echo got:$line")
    )
    events: list = []
    session.start(100, 30)
    session.attach(lambda kind, payload: events.append((kind, payload)))
    try:
        await _collect_until(events, lambda: b"30 100" in _output(events))
        session.write(b"hello\r")
        await _collect_until(events, lambda: b"got:hello" in _output(events))
        await _collect_until(events, lambda: ("exit", 0) in events)
    finally:
        session.close()
    assert not session.running


async def test_session_resize_signals_the_foreground_group():
    session = TerminalSession(
        "t2",
        _shell_launch(
            "trap 'stty size' WINCH; echo ready; while :; do sleep 0.05; done"
        ),
    )
    events: list = []
    session.start(80, 24)
    session.attach(lambda kind, payload: events.append((kind, payload)))
    try:
        await _collect_until(events, lambda: b"ready" in _output(events))
        session.resize(132, 40)
        await _collect_until(events, lambda: b"40 132" in _output(events))
    finally:
        session.close()


async def test_reattach_replays_buffered_output_and_idle_close_is_deferred():
    registry = TerminalRegistry()
    session = registry.open("t3", _shell_launch("echo first; sleep 30"), 80, 24)
    events: list = []

    def listener(kind, payload):
        events.append((kind, payload))

    session.attach(listener)
    try:
        await _collect_until(events, lambda: b"first" in _output(events))
        session.detach(listener)
        # No viewer: still running inside the idle grace period.
        assert registry.get("t3") is session
        assert session.running
        replay = session.attach(listener)
        assert b"first" in replay
    finally:
        registry.close_all()
    assert registry.get("t3") is None


async def test_registry_open_replaces_existing_session():
    registry = TerminalRegistry()
    first = registry.open("t4", _shell_launch("sleep 30"), 80, 24)
    second = registry.open("t4", _shell_launch("sleep 30"), 80, 24)
    try:
        assert not first.running
        assert registry.get("t4") is second
    finally:
        registry.close_all()


# -- consumer ----------------------------------------------------------------


async def _connect(thread_id: str, query: bytes = b"cols=90&rows=20"):
    scope = {
        "type": "websocket",
        "path": f"/ws/threads/{thread_id}/terminal/",
        "headers": [],
        "query_string": query,
        "user": "authenticated",
        "url_route": {"kwargs": {"thread_id": thread_id}},
    }
    communicator = ApplicationCommunicator(
        consumers.ThreadTerminalConsumer.as_asgi(), scope
    )
    await communicator.send_input({"type": "websocket.connect"})
    accepted = await communicator.receive_output(timeout=5)
    assert accepted["type"] == "websocket.accept"
    return communicator


async def _next_control(communicator) -> dict:
    while True:
        frame = await communicator.receive_output(timeout=5)
        if frame.get("text") is not None:
            return json.loads(frame["text"])


async def _read_bytes_until(communicator, needle: bytes) -> bytes:
    seen = b""
    while needle not in seen:
        frame = await communicator.receive_output(timeout=5)
        if frame.get("bytes") is not None:
            seen += frame["bytes"]
    return seen


def _manager_for(thread):
    manager = MagicMock()
    manager.get_thread_state = AsyncMock(return_value=thread)
    return manager


async def test_consumer_launches_then_reattaches_without_relaunching():
    registry = TerminalRegistry()
    thread = SimpleNamespace(
        backend="codex", backend_session_id=None, directory=str(Path.home())
    )
    launches: list = []

    def fake_resolve(**kwargs):
        launches.append(kwargs)
        return _shell_launch("stty size; cat")

    with (
        patch.object(consumers, "get_terminal_registry", return_value=registry),
        patch.object(
            consumers, "get_session_manager", return_value=_manager_for(thread)
        ),
        patch.object(consumers, "resolve_terminal_launch", side_effect=fake_resolve),
        patch.object(consumers, "terminal_supported", return_value=True),
    ):
        try:
            first = await _connect("thread-a")
            ready = await _next_control(first)
            assert ready["type"] == "ready"
            assert ready["data"]["reattached"] is False
            assert ready["data"]["command"] == "sh -c 'stty size; cat'"
            await _read_bytes_until(first, b"20 90")

            await first.send_input({"type": "websocket.receive", "bytes": b"ping\r"})
            await _read_bytes_until(first, b"ping")
            await first.send_input({"type": "websocket.disconnect", "code": 1000})
            await first.wait(timeout=5)

            second = await _connect("thread-a")
            ready = await _next_control(second)
            assert ready["data"]["reattached"] is True
            replay = await _read_bytes_until(second, b"ping")
            assert b"20 90" in replay
            assert len(launches) == 1
            await second.send_input({"type": "websocket.disconnect", "code": 1000})
            await second.wait(timeout=5)
        finally:
            registry.close_all()


async def test_consumer_reports_unavailable_thread():
    thread = SimpleNamespace(
        backend="claude_code", backend_session_id=None, directory=None
    )
    with (
        patch.object(
            consumers, "get_terminal_registry", return_value=TerminalRegistry()
        ),
        patch.object(
            consumers, "get_session_manager", return_value=_manager_for(thread)
        ),
        patch.object(consumers, "terminal_supported", return_value=True),
        patch.object(
            thread_terminal, "find_backend_binary", return_value=Path("/bin/claude")
        ),
    ):
        communicator = await _connect("thread-b")
        message = await _next_control(communicator)
        assert message["type"] == "error"
        assert "no saved session" in message["data"]["message"]
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        await communicator.wait(timeout=5)


async def test_consumer_rejects_unauthenticated_clients():
    scope = {
        "type": "websocket",
        "path": "/ws/threads/x/terminal/",
        "headers": [],
        "query_string": b"",
        "user": None,
        "url_route": {"kwargs": {"thread_id": "x"}},
    }
    communicator = ApplicationCommunicator(
        consumers.ThreadTerminalConsumer.as_asgi(), scope
    )
    await communicator.send_input({"type": "websocket.connect"})
    frame = await communicator.receive_output(timeout=5)
    assert frame["type"] == "websocket.close"
    assert frame["code"] == 4001
