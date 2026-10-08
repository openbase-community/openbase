"""Hub side of `openbase-coder codex|claude` from an edge (ws/agent-terminals/)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from asgiref.testing import ApplicationCommunicator  # noqa: E402

django.setup()

from openbase_coder_cli.agent_launch import (  # noqa: E402
    AgentLaunch,
    AgentLaunchError,
)
from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    consumers,
    routing,
    thread_terminal,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_terminal import (  # noqa: E402
    TerminalLaunch,
    TerminalRegistry,
    TerminalUnavailableError,
    display_command,
    resolve_agent_terminal_launch,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY only")


# --- resolve_agent_terminal_launch -----------------------------------------


def _fake_plan(calls):
    def plan(agent, args, directory, context, *, base_env):
        calls.append((agent, args, directory, base_env))
        return AgentLaunch(
            agent,
            ["/opt/bin/codex", "-p", "openbase", *args],
            directory,
            {"TERM": base_env["TERM"]},
            ("standalone notice",),
            False,
        )

    return plan


def test_hub_launch_expands_the_edges_home_relative_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Projects" / "app").mkdir(parents=True)
    calls: list = []

    launch, notices = resolve_agent_terminal_launch(
        agent="codex",
        cwd="~/Projects/app",
        args=["fix"],
        plan=_fake_plan(calls),
        context_factory=lambda: "ctx",
    )

    assert calls[0][:3] == ("codex", ["fix"], str(tmp_path / "Projects" / "app"))
    # The PTY environment is the terminal one (TERM etc.), not the server's.
    assert calls[0][3]["TERM"] == "xterm-256color"
    assert launch.backend == "codex"
    assert launch.target == "codex"
    assert launch.cwd == str(tmp_path / "Projects" / "app")
    assert notices == ("standalone notice",)


@pytest.mark.parametrize(
    ("agent", "cwd", "args", "message"),
    [
        ("gemini", "~", [], "Unknown agent"),
        ("codex", "~/missing", [], "does not exist"),
        ("codex", "relative/dir", [], "does not exist"),
        ("codex", "", [], "No working directory"),
        ("codex", "~", "not-a-list", "list of strings"),
        ("codex", "~", [1, 2], "list of strings"),
        ("claude", "~", ["x" * (64 * 1024 + 1)], "too long"),
    ],
)
def test_hub_launch_rejects_bad_requests(
    tmp_path, monkeypatch, agent, cwd, args, message
):
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(TerminalUnavailableError, match=message):
        resolve_agent_terminal_launch(
            agent=agent,
            cwd=cwd,
            args=args,
            plan=_fake_plan([]),
            context_factory=lambda: "ctx",
        )


def test_hub_launch_surfaces_planner_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    def plan(*args, **kwargs):
        raise AgentLaunchError("Openbase's Codex profile is missing.")

    with pytest.raises(TerminalUnavailableError, match="profile is missing"):
        resolve_agent_terminal_launch(
            agent="codex", cwd="~", args=[], plan=plan, context_factory=lambda: None
        )


def test_display_command_elides_long_arguments():
    shown = display_command(["/opt/bin/claude", "--append-system-prompt", "x" * 500])

    assert shown.startswith("claude --append-system-prompt ")
    assert len(shown) < 120


def test_agent_terminal_routes_are_registered():
    patterns = [str(p.pattern) for p in routing.websocket_urlpatterns]

    assert r"ws/agent-terminals/$" in patterns
    assert r"ws/agent-terminals/(?P<session_id>[^/]+)/$" in patterns


# --- consumer ----------------------------------------------------------------


def _shell_launch(script: str) -> TerminalLaunch:
    return TerminalLaunch(
        backend="codex",
        argv=["/bin/sh", "-c", script],
        cwd=str(Path.home()),
        env={**os.environ, "TERM": "xterm-256color"},
    )


async def _connect(session_id: str | None = None, query: bytes = b"cols=90&rows=20"):
    kwargs = {"session_id": session_id} if session_id else {}
    scope = {
        "type": "websocket",
        "path": f"/ws/agent-terminals/{session_id + '/' if session_id else ''}",
        "headers": [],
        "query_string": query,
        "user": "authenticated",
        "url_route": {"kwargs": kwargs},
    }
    communicator = ApplicationCommunicator(
        consumers.AgentTerminalConsumer.as_asgi(), scope
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


async def _start(communicator, **overrides) -> None:
    message = {
        "type": "start",
        "agent": "codex",
        "cwd": "~",
        "args": ["fix"],
        "cols": 100,
        "rows": 30,
        **overrides,
    }
    await communicator.send_input(
        {"type": "websocket.receive", "text": json.dumps(message)}
    )


async def _close(communicator) -> None:
    await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
    await communicator.wait(timeout=5)


async def test_start_launches_a_session_and_a_client_can_reattach_by_id():
    registry = TerminalRegistry()
    requests: list = []

    def fake_resolve(**kwargs):
        requests.append(kwargs)
        return _shell_launch("stty size; cat"), ("hub notice",)

    with (
        patch.object(consumers, "get_terminal_registry", return_value=registry),
        patch.object(
            consumers, "resolve_agent_terminal_launch", side_effect=fake_resolve
        ),
        patch.object(consumers, "terminal_supported", return_value=True),
    ):
        try:
            first = await _connect()
            # Nothing happens until the client says what to start.
            await asyncio.sleep(0.1)
            assert first.output_queue.empty()
            await _start(first)
            ready = await _next_control(first)
            assert ready["type"] == "ready"
            session_id = ready["data"]["id"]
            assert session_id.startswith("agent-")
            assert ready["data"]["notices"] == ["hub notice"]
            assert ready["data"]["reattached"] is False
            assert requests == [{"agent": "codex", "cwd": "~", "args": ["fix"]}]
            # The start message's size wins over the query string.
            await _read_bytes_until(first, b"30 100")

            await first.send_input({"type": "websocket.receive", "bytes": b"ping\r"})
            await _read_bytes_until(first, b"ping")
            await _close(first)
            assert registry.get(session_id).running

            second = await _connect(session_id, b"cols=90&rows=20&replay=0")
            ready = await _next_control(second)
            assert ready["data"]["reattached"] is True
            assert ready["data"]["id"] == session_id
            # replay=0: no buffered output, only what happens next.
            await second.send_input({"type": "websocket.receive", "bytes": b"pong\r"})
            seen = await _read_bytes_until(second, b"pong")
            assert b"ping" not in seen
            assert len(requests) == 1
            await _close(second)
        finally:
            registry.close_all()


async def test_start_errors_are_reported_to_the_client():
    with (
        patch.object(
            consumers, "get_terminal_registry", return_value=TerminalRegistry()
        ),
        patch.object(
            consumers,
            "resolve_agent_terminal_launch",
            side_effect=TerminalUnavailableError(
                "~/x does not exist on this computer."
            ),
        ),
        patch.object(consumers, "terminal_supported", return_value=True),
    ):
        communicator = await _connect()
        await _start(communicator, cwd="~/x")
        message = await _next_control(communicator)
        assert message == {
            "type": "error",
            "data": {"message": "~/x does not exist on this computer."},
        }
        await _close(communicator)


@pytest.mark.parametrize("session_id", ["agent-gone", "thread-1"])
async def test_reattaching_to_an_ended_or_foreign_session_is_an_error(session_id):
    with (
        patch.object(
            consumers, "get_terminal_registry", return_value=TerminalRegistry()
        ),
        patch.object(consumers, "terminal_supported", return_value=True),
    ):
        communicator = await _connect(session_id)
        message = await _next_control(communicator)
        assert message["type"] == "error"
        await _close(communicator)


async def test_unauthenticated_clients_are_rejected():
    scope = {
        "type": "websocket",
        "path": "/ws/agent-terminals/",
        "headers": [],
        "query_string": b"",
        "user": None,
        "url_route": {"kwargs": {}},
    }
    communicator = ApplicationCommunicator(
        consumers.AgentTerminalConsumer.as_asgi(), scope
    )
    await communicator.send_input({"type": "websocket.connect"})
    frame = await communicator.receive_output(timeout=5)
    assert frame["type"] == "websocket.close"
    assert frame["code"] == 4001


def test_thread_terminal_still_strips_inherited_session_markers(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")

    assert "CLAUDECODE" not in thread_terminal._terminal_env()
