"""MCP gateway: bridge, consumer, relay, profile offers, and CLI (no network)."""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import os
import socket
import sys
import textwrap
import time
from pathlib import Path

import pytest

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402

django.setup()

import tomlkit  # noqa: E402
from asgiref.testing import ApplicationCommunicator  # noqa: E402
from channels.routing import URLRouter  # noqa: E402
from click.testing import CliRunner  # noqa: E402

from openbase_coder_cli import mcp_gateway as gw  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    consumers,
    middleware,
)
from openbase_coder_cli.openbase_coder_cli_app.routing import (  # noqa: E402
    websocket_urlpatterns,
)

# The package re-exports the click group under the module's own name.
gw_cli = importlib.import_module("openbase_coder_cli.cli.mcp_gateway")

FAKE_SERVER = textwrap.dedent(
    """
    import json, os, sys, time

    mode = sys.argv[1] if len(sys.argv) > 1 else "echo"
    pid_file = os.environ.get("FAKE_MCP_PID_FILE")
    if pid_file:
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
    if mode == "exit":
        sys.exit(0)
    if mode == "spawn-child-exit":
        import subprocess
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if pid_file:
            with open(pid_file, "w") as f:
                f.write(f"{os.getpid()} {child.pid}")
        sys.exit(0)
    if mode == "stubborn":
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sys.stdout.write("ready\\n"); sys.stdout.flush()
        while True:
            time.sleep(1)
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("method") == "quit":
            sys.exit(0)
        if "id" in msg:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {"echo": msg.get("params")}}
            sys.stdout.write(json.dumps(reply) + "\\n")
            sys.stdout.flush()
    """
)


@pytest.fixture
def fake_server(tmp_path: Path) -> Path:
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    return script


@pytest.fixture
def gateway_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "mcp-gateway.json"
    monkeypatch.setattr(gw, "GATEWAY_CONFIG_PATH", path)
    return path


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


# --- config -----------------------------------------------------------------


def test_serve_add_builtin_custom_and_remove(gateway_config: Path) -> None:
    computer = gw.serve_add("computer")
    assert computer.command == ["openbase-coder", "claude", "computer-use-mcp"]
    gw.serve_add("browser", ["npx", "browser-mcp", "--flag"], "a browser")
    served = gw.served_servers()
    assert set(served) == {"computer", "browser"}
    assert served["browser"].command == ["npx", "browser-mcp", "--flag"]
    assert oct(gateway_config.stat().st_mode & 0o777) == "0o600"

    assert gw.serve_remove("browser") is True
    assert gw.serve_remove("browser") is False
    assert set(gw.served_servers()) == {"computer"}


@pytest.mark.parametrize(
    "name", ["", "a/b", "computer-laptop", "-x", "a b", "x?y", "a" * 64]
)
def test_serve_add_rejects_bad_names(gateway_config: Path, name: str) -> None:
    with pytest.raises(ValueError):
        gw.serve_add(name, ["true"])


def test_serve_add_unknown_builtin_needs_command(gateway_config: Path) -> None:
    with pytest.raises(ValueError, match="not a built-in"):
        gw.serve_add("browser")


def test_served_servers_ignores_malformed_entries(gateway_config: Path) -> None:
    gateway_config.write_text(
        json.dumps(
            {
                "servers": {
                    "ok": {"command": ["true"]},
                    "empty": {"command": []},
                    "notalist": {"command": "true"},
                    "bad name": {"command": ["true"]},
                    "nondict": "x",
                }
            }
        )
    )
    assert set(gw.served_servers()) == {"ok"}
    gateway_config.write_text("not json")
    assert gw.served_servers() == {}


# --- bridge -----------------------------------------------------------------


async def test_bridge_round_trip_and_normalizes_frames(fake_server: Path) -> None:
    received: asyncio.Queue[str] = asyncio.Queue()
    server = gw.ServedServer("fake", [sys.executable, str(fake_server)])
    bridge = gw.GatewayBridge(server, received.put)
    await bridge.start()
    try:
        assert await bridge.to_process(
            json.dumps({"jsonrpc": "2.0", "id": 1, "params": {"a": 1}})
        )
        reply = json.loads(await asyncio.wait_for(received.get(), 5))
        assert reply == {"jsonrpc": "2.0", "id": 1, "result": {"echo": {"a": 1}}}

        # A pretty-printed frame still reaches the process as one line.
        pretty = json.dumps({"jsonrpc": "2.0", "id": 2, "params": "x"}, indent=2)
        assert "\n" in pretty
        assert await bridge.to_process(pretty)
        reply = json.loads(await asyncio.wait_for(received.get(), 5))
        assert reply["id"] == 2

        # Non-JSON multi-line garbage is dropped, not forwarded half-way.
        assert await bridge.to_process("{not\njson")
    finally:
        await bridge.stop()
    assert bridge.proc is not None and bridge.proc.returncode is not None


async def test_bridge_reports_process_exit(fake_server: Path) -> None:
    server = gw.ServedServer("fake", [sys.executable, str(fake_server), "exit"])
    bridge = gw.GatewayBridge(server, lambda text: asyncio.sleep(0))
    await bridge.start()
    await asyncio.wait_for(bridge.wait_stdout_closed(), 5)
    await bridge.stop()
    assert not await bridge.to_process('{"jsonrpc":"2.0","id":1}')


async def test_bridge_stop_kills_process_that_ignores_eof_and_sigterm(
    fake_server: Path,
) -> None:
    received: asyncio.Queue[str] = asyncio.Queue()
    server = gw.ServedServer("fake", [sys.executable, str(fake_server), "stubborn"])
    bridge = gw.GatewayBridge(server, received.put)
    await bridge.start()
    assert await asyncio.wait_for(received.get(), 5) == "ready"
    pid = bridge.proc.pid
    started = time.monotonic()
    await bridge.stop()
    assert time.monotonic() - started < 5
    assert await _wait_dead(pid)


async def test_bridge_stop_kills_process_group_after_server_exits(
    fake_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("FAKE_MCP_PID_FILE", str(pid_file))
    server = gw.ServedServer(
        "fake", [sys.executable, str(fake_server), "spawn-child-exit"]
    )
    bridge = gw.GatewayBridge(server, lambda text: asyncio.sleep(0))
    child_pid = None
    await bridge.start()
    try:
        await _wait_for(lambda: pid_file.exists())
        assert bridge.proc is not None
        await asyncio.wait_for(bridge.proc.wait(), 5)
        _, child_pid_text = pid_file.read_text().split()
        child_pid = int(child_pid_text)
        assert _pid_alive(child_pid)
        await bridge.stop()
        assert await _wait_dead(child_pid)
    finally:
        if child_pid is not None and _pid_alive(child_pid):
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass


async def test_bridge_start_failure_raises_oserror(tmp_path: Path) -> None:
    server = gw.ServedServer("missing", [str(tmp_path / "no-such-binary")])
    bridge = gw.GatewayBridge(server, lambda text: asyncio.sleep(0))
    with pytest.raises(OSError):
        await bridge.start()


def test_resolve_command_maps_openbase_coder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw, "_openbase_coder_path", lambda: "/opt/ob/openbase-coder")
    assert gw._resolve_command(["openbase-coder", "claude", "computer-use-mcp"]) == [
        "/opt/ob/openbase-coder",
        "claude",
        "computer-use-mcp",
    ]
    assert gw._resolve_command(["npx", "x"]) == ["npx", "x"]


# --- consumer ---------------------------------------------------------------


def _scope(name: str, user: str | None = "authenticated") -> dict:
    return {
        "type": "websocket",
        "path": f"/ws/mcp-gateway/{name}/",
        "headers": [],
        "user": user,
        "url_route": {"kwargs": {"name": name}},
    }


async def _open(name: str, user: str | None = "authenticated"):
    communicator = ApplicationCommunicator(
        consumers.McpGatewayConsumer.as_asgi(), _scope(name, user)
    )
    await communicator.send_input({"type": "websocket.connect"})
    return communicator, await communicator.receive_output(timeout=5)


async def test_consumer_rejects_unauthenticated(
    gateway_config: Path, fake_server: Path
) -> None:
    gw.serve_add("fake", [sys.executable, str(fake_server)])
    communicator, frame = await _open("fake", user=None)
    assert frame == {"type": "websocket.close", "code": gw.CLOSE_UNAUTHENTICATED}
    await communicator.send_input({"type": "websocket.disconnect", "code": 4001})
    await communicator.wait(timeout=5)


async def test_consumer_rejects_unknown_server(gateway_config: Path) -> None:
    gw.serve_add("computer")
    communicator, frame = await _open("browser")
    assert frame["type"] == "websocket.accept"
    closed = await communicator.receive_output(timeout=5)
    assert closed["type"] == "websocket.close"
    assert closed["code"] == gw.CLOSE_UNKNOWN_SERVER
    await communicator.send_input({"type": "websocket.disconnect", "code": 4004})
    await communicator.wait(timeout=5)


async def test_consumer_reports_start_failure(
    gateway_config: Path, tmp_path: Path
) -> None:
    gw.serve_add("broken", [str(tmp_path / "no-such-binary")])
    communicator, frame = await _open("broken")
    assert frame["type"] == "websocket.accept"
    closed = await communicator.receive_output(timeout=5)
    assert closed["code"] == gw.CLOSE_START_FAILED
    await communicator.send_input({"type": "websocket.disconnect", "code": 4500})
    await communicator.wait(timeout=5)


async def test_consumer_round_trip_and_kills_child_on_disconnect(
    gateway_config: Path,
    fake_server: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("FAKE_MCP_PID_FILE", str(pid_file))
    gw.serve_add("fake", [sys.executable, str(fake_server)])
    communicator, frame = await _open("fake")
    assert frame["type"] == "websocket.accept"

    await communicator.send_input(
        {
            "type": "websocket.receive",
            "text": json.dumps({"jsonrpc": "2.0", "id": 7, "params": [1]}),
        }
    )
    out = await communicator.receive_output(timeout=5)
    assert out["type"] == "websocket.send"
    assert json.loads(out["text"]) == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"echo": [1]},
    }
    # Binary frames are accepted as UTF-8 text.
    await communicator.send_input(
        {"type": "websocket.receive", "bytes": b'{"jsonrpc":"2.0","id":8}'}
    )
    out = await communicator.receive_output(timeout=5)
    assert json.loads(out["text"])["id"] == 8

    pid = int(pid_file.read_text())
    assert _pid_alive(pid)
    await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
    await communicator.wait(timeout=10)
    assert await _wait_dead(pid)


async def test_consumer_closes_when_process_exits(
    gateway_config: Path, fake_server: Path
) -> None:
    gw.serve_add("fake", [sys.executable, str(fake_server)])
    communicator, frame = await _open("fake")
    assert frame["type"] == "websocket.accept"
    await communicator.send_input(
        {"type": "websocket.receive", "text": '{"jsonrpc":"2.0","method":"quit"}'}
    )
    closed = await communicator.receive_output(timeout=5)
    assert closed["type"] == "websocket.close"
    assert closed["code"] == gw.CLOSE_PROCESS_EXITED
    await communicator.send_input({"type": "websocket.disconnect", "code": 4502})
    await communicator.wait(timeout=5)


def test_route_accepts_only_valid_names() -> None:
    router = URLRouter(websocket_urlpatterns)
    match = None
    for pattern in router.routes:
        resolved = pattern.pattern.match("ws/mcp-gateway/computer/")
        if resolved:
            match = resolved
    assert match is not None and match[2]["name"] == "computer"
    for bad in ("ws/mcp-gateway/a b/", "ws/mcp-gateway/../", "ws/mcp-gateway//"):
        assert not any(p.pattern.match(bad) for p in router.routes)


async def test_routed_socket_uses_token_auth(
    gateway_config: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real middleware + router: a wrong token is rejected before accept."""
    monkeypatch.setattr(middleware, "get_local_api_token", lambda: "local-token")
    gw.serve_add("fake", [sys.executable, str(fake_server)])
    app = middleware.TokenAuthMiddleware(URLRouter(websocket_urlpatterns))

    async def open_with(token: str):
        scope = {
            "type": "websocket",
            "path": "/ws/mcp-gateway/fake/",
            "query_string": f"token={token}".encode(),
            "headers": [],
        }
        communicator = ApplicationCommunicator(app, scope)
        await communicator.send_input({"type": "websocket.connect"})
        return communicator, await communicator.receive_output(timeout=5)

    communicator, frame = await open_with("wrong")
    assert frame == {"type": "websocket.close", "code": gw.CLOSE_UNAUTHENTICATED}
    await communicator.send_input({"type": "websocket.disconnect", "code": 4001})
    await communicator.wait(timeout=5)

    communicator, frame = await open_with("local-token")
    assert frame["type"] == "websocket.accept"
    await communicator.send_input(
        {"type": "websocket.receive", "text": '{"jsonrpc":"2.0","id":1}'}
    )
    out = await communicator.receive_output(timeout=5)
    assert json.loads(out["text"])["id"] == 1
    await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
    await communicator.wait(timeout=10)


# --- requesting side --------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://laptop.tail:18080", "ws://laptop.tail:18080/ws/mcp-gateway/computer/"),
        ("https://laptop.tail/", "wss://laptop.tail/ws/mcp-gateway/computer/"),
        ("laptop:18080", "ws://laptop:18080/ws/mcp-gateway/computer/"),
        ("http://[fd7a::1]:18080", "ws://[fd7a::1]:18080/ws/mcp-gateway/computer/"),
        ("ws://127.0.0.1:5", "ws://127.0.0.1:5/ws/mcp-gateway/computer/"),
    ],
)
def test_peer_ws_url(base: str, expected: str) -> None:
    assert gw.peer_ws_url(base, "computer", "tok") == f"{expected}?token=tok"


def test_peer_ws_url_quotes_name_and_token() -> None:
    url = gw.peer_ws_url("http://h:1", "a b", "x.y+z/=")
    assert url == "ws://h:1/ws/mcp-gateway/a%20b/?token=x.y%2Bz%2F%3D"


class _Collector(io.BytesIO):
    """A binary stdout whose contents tests can poll."""

    def lines(self) -> list[bytes]:
        return [line for line in self.getvalue().split(b"\n") if line]


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_relay_round_trip_and_clean_exit_on_stdin_eof() -> None:
    from websockets.asyncio.server import serve

    async def echo(ws):
        async for message in ws:
            await ws.send(json.dumps({"got": json.loads(message)}))

    async with serve(echo, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        read_fd, write_fd = os.pipe()
        stdout, stderr = _Collector(), io.StringIO()
        relay = asyncio.create_task(
            gw.relay_stdio(
                f"ws://127.0.0.1:{port}/",
                stdin_fd=read_fd,
                stdout=stdout,
                stderr=stderr,
            )
        )
        os.write(write_fd, b'{"id":1}\n\n{"id":')
        os.write(write_fd, b"2}\n")
        await _wait_for(lambda: len(stdout.lines()) == 2)
        assert [json.loads(line) for line in stdout.lines()] == [
            {"got": {"id": 1}},
            {"got": {"id": 2}},
        ]
        os.close(write_fd)
        assert await asyncio.wait_for(relay, 5) == gw.EXIT_OK
        os.close(read_fd)


async def test_relay_exits_unavailable_when_peer_closes_bridge() -> None:
    from websockets.asyncio.server import serve

    async def reject(ws):
        await ws.close(gw.CLOSE_UNKNOWN_SERVER, "browser is not served on this machine")

    async with serve(reject, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        read_fd, write_fd = os.pipe()
        stderr = io.StringIO()
        code = await asyncio.wait_for(
            gw.relay_stdio(
                f"ws://127.0.0.1:{port}/",
                stdin_fd=read_fd,
                stdout=_Collector(),
                stderr=stderr,
            ),
            5,
        )
        # stdin is still open: the relay must not wait for the client.
        assert code == gw.EXIT_UNAVAILABLE
        assert "4004" in stderr.getvalue()
        assert "not served" in stderr.getvalue()
        os.close(write_fd)
        os.close(read_fd)


async def test_relay_process_exits_promptly_while_client_holds_stdin_open() -> None:
    """A real process whose peer closes must exit even though stdin stays open.

    Guards the stdin reader: a blocked executor read would keep the
    interpreter alive at shutdown and the agent would see a hung server.
    """
    from websockets.asyncio.server import serve

    async def reject(ws):
        await ws.close(gw.CLOSE_PROCESS_EXITED, "fake exited")

    async with serve(reject, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        code = (
            "import asyncio, sys\n"
            "from openbase_coder_cli import mcp_gateway as gw\n"
            f"sys.exit(asyncio.run(gw.relay_stdio('ws://127.0.0.1:{port}/')))\n"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            # Keep stdin open (communicate() would close it): only the peer's
            # close may end the process.
            await asyncio.wait_for(proc.wait(), 20)
            out = await proc.stdout.read()
            err = await proc.stderr.read()
        finally:
            if proc.returncode is None:
                proc.kill()
            proc.stdin.close()
    assert proc.returncode == gw.EXIT_UNAVAILABLE, err.decode()
    assert out == b""
    assert b"4502" in err
    assert b"Fatal Python error" not in err


async def test_relay_exits_unavailable_when_peer_is_down() -> None:
    port = _free_port()
    stdout, stderr = _Collector(), io.StringIO()
    started = time.monotonic()
    code = await gw.relay_stdio(
        f"ws://127.0.0.1:{port}/", stdin_fd=0, stdout=stdout, stderr=stderr
    )
    assert code == gw.EXIT_UNAVAILABLE
    assert time.monotonic() - started < 5
    assert stdout.getvalue() == b""
    assert "peer unavailable" in stderr.getvalue()


async def test_relay_reports_refused_handshake() -> None:
    from websockets.asyncio.server import serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    def forbid(connection, request):
        return Response(403, "Forbidden", Headers(), b"")

    async def never(ws):  # pragma: no cover - handshake is refused
        await ws.close()

    async with serve(never, "127.0.0.1", 0, process_request=forbid) as server:
        port = server.sockets[0].getsockname()[1]
        stderr = io.StringIO()
        code = await gw.relay_stdio(
            f"ws://127.0.0.1:{port}/", stdin_fd=0, stdout=_Collector(), stderr=stderr
        )
    assert code == gw.EXIT_UNAVAILABLE
    assert "HTTP 403" in stderr.getvalue()


async def test_relay_end_to_end_through_real_asgi_server(
    gateway_config: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hub relay -> uvicorn (middleware + router + consumer) -> stdio server."""
    import uvicorn

    monkeypatch.setattr(middleware, "get_local_api_token", lambda: "local-token")
    gw.serve_add("fake", [sys.executable, str(fake_server)])
    app = middleware.TokenAuthMiddleware(URLRouter(websocket_urlpatterns))

    async def asgi(scope, receive, send):
        if scope["type"] == "lifespan":
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        await app(scope, receive, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(asgi, log_level="warning", ws="websockets-sansio")
    )
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await _wait_for(lambda: server.started)
        read_fd, write_fd = os.pipe()
        stdout, stderr = _Collector(), io.StringIO()
        url = gw.peer_ws_url(f"http://127.0.0.1:{port}", "fake", "local-token")
        relay = asyncio.create_task(
            gw.relay_stdio(url, stdin_fd=read_fd, stdout=stdout, stderr=stderr)
        )
        os.write(write_fd, b'{"jsonrpc":"2.0","id":3,"params":"hi"}\n')
        await _wait_for(lambda: stdout.lines())
        assert json.loads(stdout.lines()[0])["result"] == {"echo": "hi"}
        os.close(write_fd)
        assert await asyncio.wait_for(relay, 10) == gw.EXIT_OK
        os.close(read_fd)

        # Not served -> the relay exits instead of hanging.
        read_fd, write_fd = os.pipe()
        stderr = io.StringIO()
        url = gw.peer_ws_url(f"http://127.0.0.1:{port}", "other", "local-token")
        code = await asyncio.wait_for(
            gw.relay_stdio(url, stdin_fd=read_fd, stdout=_Collector(), stderr=stderr),
            10,
        )
        assert code == gw.EXIT_UNAVAILABLE
        assert str(gw.CLOSE_UNKNOWN_SERVER) in stderr.getvalue()
        os.close(write_fd)
        os.close(read_fd)

        # Wrong token -> refused handshake.
        stderr = io.StringIO()
        url = gw.peer_ws_url(f"http://127.0.0.1:{port}", "fake", "wrong")
        code = await gw.relay_stdio(url, stdin_fd=0, stdout=_Collector(), stderr=stderr)
        assert code == gw.EXIT_UNAVAILABLE
        assert "HTTP 403" in stderr.getvalue()
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)


# --- offers -----------------------------------------------------------------

SHIM = "/opt/ob/bin/openbase-coder"


@pytest.fixture
def profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gw, "_openbase_coder_path", lambda: SHIM)
    claude = tmp_path / "profiles" / "claude" / "mcp.json"
    claude.parent.mkdir(parents=True)
    claude.write_text(
        json.dumps({"mcpServers": {"super-agents": {"command": "sa", "args": []}}})
    )
    codex = tmp_path / "codex" / "openbase.config.toml"
    codex.parent.mkdir()
    codex.write_text(
        textwrap.dedent(
            """\
            model = "gpt-5"

            [mcp_servers.super-agents]
            command = "sa"
            args = []
            """
        )
    )
    cloud = tmp_path / "codex" / "openbase-cloud.config.toml"  # absent
    monkeypatch.setattr(gw, "CLAUDE_PROFILE_MCP_PATH", claude)
    monkeypatch.setattr(gw, "CODEX_PROFILE_PATH", codex)
    monkeypatch.setattr(gw, "CLOUD_CODEX_PROFILE_PATH", cloud)
    return claude, codex, cloud


def test_offer_writes_claude_and_existing_codex_profiles(profiles) -> None:
    claude, codex, cloud = profiles
    changed = gw.offer("computer", "gabes-laptop")
    assert changed == [claude, codex]
    assert not cloud.exists()

    data = json.loads(claude.read_text())
    assert data["mcpServers"]["super-agents"] == {"command": "sa", "args": []}
    assert data["mcpServers"]["computer-laptop"] == {
        "type": "stdio",
        "command": SHIM,
        "args": ["mcp-gateway", "connect", "computer", "--peer", "gabes-laptop"],
    }
    doc = tomlkit.parse(codex.read_text())
    assert doc["model"] == "gpt-5"
    assert doc["mcp_servers"]["super-agents"]["command"] == "sa"
    entry = doc["mcp_servers"]["computer-laptop"]
    assert entry["command"] == SHIM
    assert list(entry["args"]) == [
        "mcp-gateway",
        "connect",
        "computer",
        "--peer",
        "gabes-laptop",
    ]

    # Idempotent; a different peer rewrites the entry.
    assert gw.offer("computer", "gabes-laptop") == []
    assert gw.offer("computer", "other-mac") == [claude, codex]
    assert gw.offered()["computer-laptop"].peer == "other-mac"


def test_offer_creates_codex_table_when_profile_has_none(profiles) -> None:
    claude, codex, _ = profiles
    codex.write_text('model = "gpt-5"\n')
    gw.offer("computer", "laptop")
    doc = tomlkit.parse(codex.read_text())
    assert doc["model"] == "gpt-5"
    assert doc["mcp_servers"]["computer-laptop"]["command"] == SHIM
    assert "[mcp_servers.computer-laptop]" in codex.read_text()


def test_offered_lists_both_profiles(profiles) -> None:
    claude, codex, _ = profiles
    assert gw.offered() == {}
    gw.offer("computer", "laptop")
    gw.offer("browser", "laptop")
    offered = gw.offered()
    assert list(offered) == ["browser-laptop", "computer-laptop"]
    assert offered["computer-laptop"] == gw.OfferedServer(
        key="computer-laptop", name="computer", peer="laptop", profiles=(claude, codex)
    )


def test_withdraw_removes_only_gateway_entries(profiles) -> None:
    claude, codex, _ = profiles
    gw.offer("computer", "laptop")
    assert gw.withdraw("computer") == [claude, codex]
    assert "computer-laptop" not in json.loads(claude.read_text())["mcpServers"]
    assert "computer-laptop" not in tomlkit.parse(codex.read_text())["mcp_servers"]
    assert gw.withdraw("computer") == []
    assert "super-agents" in json.loads(claude.read_text())["mcpServers"]

    # A user's own server that happens to use the name is never touched.
    data = json.loads(claude.read_text())
    data["mcpServers"]["mine-laptop"] = {"command": "my-own-server", "args": []}
    claude.write_text(json.dumps(data))
    assert gw.withdraw("mine") == []
    assert "mine-laptop" in json.loads(claude.read_text())["mcpServers"]
    with pytest.raises(ValueError, match="different MCP server"):
        gw.offer("mine", "laptop")


def test_offer_refuses_invalid_profile_without_clobbering(profiles) -> None:
    claude, codex, _ = profiles
    claude.write_text("{ not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        gw.offer("computer", "laptop")
    assert claude.read_text() == "{ not json"
    assert "computer-laptop" not in codex.read_text()

    claude.write_text("{}")
    codex.write_text("this is = = not toml")
    with pytest.raises(ValueError, match="not valid TOML"):
        gw.offer("computer", "laptop")
    assert claude.read_text() == "{}"


def test_offer_validates_name_and_peer(profiles) -> None:
    with pytest.raises(ValueError):
        gw.offer("computer-laptop", "laptop")
    with pytest.raises(ValueError):
        gw.offer("computer", "")


def test_offer_creates_missing_claude_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gw, "_openbase_coder_path", lambda: SHIM)
    claude = tmp_path / "new" / "mcp.json"
    assert gw.offer("computer", "laptop", claude_path=claude, codex_paths=[]) == [
        claude
    ]
    assert "computer-laptop" in json.loads(claude.read_text())["mcpServers"]


# --- CLI --------------------------------------------------------------------


def test_cli_serve_add_list_remove(gateway_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(gw_cli.mcp_gateway, ["serve", "list"])
    assert result.exit_code == 0
    assert "Not serving" in result.output
    assert "computer" in result.output

    result = runner.invoke(gw_cli.mcp_gateway, ["serve", "add", "computer"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        gw_cli.mcp_gateway,
        ["serve", "add", "browser", "--", "npx", "browser-mcp", "--headless"],
    )
    assert result.exit_code == 0, result.output
    assert gw.served_servers()["browser"].command == [
        "npx",
        "browser-mcp",
        "--headless",
    ]

    result = runner.invoke(gw_cli.mcp_gateway, ["serve", "list"])
    assert "computer: openbase-coder claude computer-use-mcp" in result.output
    assert "browser: npx browser-mcp --headless" in result.output

    result = runner.invoke(gw_cli.mcp_gateway, ["serve", "add", "nope"])
    assert result.exit_code != 0
    assert "not a built-in" in result.output

    result = runner.invoke(gw_cli.mcp_gateway, ["serve", "remove", "browser"])
    assert result.exit_code == 0
    assert set(gw.served_servers()) == {"computer"}


def test_cli_connect_exits_unavailable_when_peer_offline(monkeypatch) -> None:
    from openbase_coder_cli.services import fleet_aggregation

    monkeypatch.setattr(fleet_aggregation, "find_peer", lambda name: None)
    result = CliRunner().invoke(
        gw_cli.mcp_gateway, ["connect", "computer", "--peer", "laptop"]
    )
    assert result.exit_code == gw.EXIT_UNAVAILABLE
    assert result.stdout == ""
    assert "laptop is not online" in result.stderr


def test_cli_connect_exits_unavailable_without_token(monkeypatch) -> None:
    from openbase_coder_cli.services import fleet_aggregation

    peer = fleet_aggregation.FleetPeer("laptop.tail", "laptop", "http://laptop:18080")
    monkeypatch.setattr(fleet_aggregation, "find_peer", lambda name: peer)
    monkeypatch.setattr(fleet_aggregation, "owner_access_token", lambda: None)
    result = CliRunner().invoke(
        gw_cli.mcp_gateway, ["connect", "computer", "--peer", "laptop"]
    )
    assert result.exit_code == gw.EXIT_UNAVAILABLE
    assert result.stdout == ""
    assert "not signed in" in result.stderr


def test_cli_connect_relays_to_peer_url(monkeypatch) -> None:
    from openbase_coder_cli.services import fleet_aggregation

    peer = fleet_aggregation.FleetPeer("laptop.tail", "laptop", "http://laptop:18080")
    monkeypatch.setattr(fleet_aggregation, "find_peer", lambda name: peer)
    monkeypatch.setattr(fleet_aggregation, "owner_access_token", lambda: "a.b.c")
    seen = {}

    async def fake_relay(url, *, open_timeout):
        seen["url"], seen["timeout"] = url, open_timeout
        return 0

    monkeypatch.setattr(gw, "relay_stdio", fake_relay)
    result = CliRunner().invoke(
        gw_cli.mcp_gateway,
        ["connect", "computer", "--peer", "laptop", "--timeout", "2"],
    )
    assert result.exit_code == 0
    assert seen == {
        "url": "ws://laptop:18080/ws/mcp-gateway/computer/?token=a.b.c",
        "timeout": 2.0,
    }


def test_cli_offer_withdraw_offered(profiles) -> None:
    runner = CliRunner()
    result = runner.invoke(gw_cli.mcp_gateway, ["offered"])
    assert "No gateway servers offered" in result.output

    result = runner.invoke(
        gw_cli.mcp_gateway, ["offer", "computer", "--peer", "laptop"]
    )
    assert result.exit_code == 0, result.output
    assert "Offered computer-laptop" in result.output

    result = runner.invoke(gw_cli.mcp_gateway, ["offered"])
    assert "computer-laptop: computer on laptop" in result.output

    result = runner.invoke(gw_cli.mcp_gateway, ["withdraw", "computer"])
    assert result.exit_code == 0
    assert "Withdrew computer-laptop" in result.output
    result = runner.invoke(gw_cli.mcp_gateway, ["withdraw", "computer"])
    assert "was not offered" in result.output

    result = runner.invoke(gw_cli.mcp_gateway, ["offer", "bad/name", "--peer", "x"])
    assert result.exit_code != 0


def test_mcp_gateway_registered_on_main() -> None:
    from openbase_coder_cli.cli import main

    assert main.get_command(None, "mcp-gateway") is gw_cli.mcp_gateway
