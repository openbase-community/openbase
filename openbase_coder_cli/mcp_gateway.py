"""MCP gateway: reach a machine-bound MCP server on another of your devices.

Some MCP servers only make sense where they run: computer control acts on
the screen you are looking at, a browser extension's server lives where the
browser is. When the agent runs on another machine (a hub), the gateway lets
it use those servers anyway, and makes the location explicit in the name:
the hub's agent sees ``computer-laptop``, not ``computer``.

The agent is in charge. Offering a server only adds it to the agent's tool
list; nothing routes calls through it, wraps commands, or prefers it. The
agent decides when a laptop-side tool is the right one.

Two halves:

* Serving side (the machine with the screen): ``McpGatewayConsumer`` accepts
  an authenticated WebSocket at ``ws/mcp-gateway/<name>/`` and bridges it to
  the stdio process of a server listed in ``~/.openbase/mcp-gateway.json``.
  One JSON-RPC message per text frame, one per line on the process side.
  Authentication is the same owner-JWT check every peer request uses.
* Requesting side (the hub): ``openbase-coder mcp-gateway connect <name>
  --peer <device>`` is a stdio MCP server that relays to that WebSocket. When
  the peer is unreachable it exits at once, so the agent's client reports the
  server unavailable instead of hanging.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
import tempfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from openbase_coder_cli.paths import (
    CLAUDE_PROFILE_MCP_PATH,
    CLOUD_CODEX_PROFILE_PATH,
    CODEX_PROFILE_PATH,
    OPENBASE_BASE_DIR,
)

logger = logging.getLogger(__name__)

GATEWAY_CONFIG_PATH = OPENBASE_BASE_DIR / "mcp-gateway.json"
GATEWAY_WS_PATH = "ws/mcp-gateway/{name}/"
OFFER_SUFFIX = "-laptop"

# Server names: what the route accepts and what lands in agent configs.
NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}"
_NAME_RE = re.compile(rf"^{NAME_PATTERN}$")

# Servers this machine knows how to serve. Serving is opt-in per server:
# `openbase-coder mcp-gateway serve add computer`.
BUILTIN_SERVERS: dict[str, dict[str, Any]] = {
    "computer": {
        "command": ["openbase-coder", "claude", "computer-use-mcp"],
        "description": "Computer control of this machine's screen (screenshots, clicks, typing).",
    },
}

MAX_FRAME_BYTES = 8 << 20  # one JSON-RPC message (screenshots are base64)

# WebSocket close codes (4000-4999 is the application range).
CLOSE_UNAUTHENTICATED = 4001
CLOSE_UNKNOWN_SERVER = 4004
CLOSE_START_FAILED = 4500
CLOSE_PROCESS_EXITED = 4502

# Exit codes of `mcp-gateway connect` (sysexits.h).
EXIT_OK = 0
EXIT_UNAVAILABLE = 69  # EX_UNAVAILABLE

_STOP_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class ServedServer:
    name: str
    command: list[str]
    description: str = ""


def validate_name(name: str) -> str:
    """A server name the route, the config, and agent configs all accept."""
    if not _NAME_RE.match(name or "") or name.endswith(OFFER_SUFFIX):
        raise ValueError(
            f"invalid server name {name!r}: use letters, digits, '.', '_' or '-' "
            f"(at most 63 characters), not ending in {OFFER_SUFFIX!r}"
        )
    return name


def _atomic_write(path: Path, content: str, *, mode: int | None = None) -> None:
    """Replace a file's content atomically, preserving a symlinked target."""
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        if mode is not None:
            temporary.chmod(mode)
        elif target.exists():
            temporary.chmod(target.stat().st_mode & 0o777)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


# --- serving side -----------------------------------------------------------


def _read_config(path: Path | None = None) -> dict[str, Any]:
    p = path or GATEWAY_CONFIG_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_config(data: dict[str, Any], path: Path | None = None) -> Path:
    p = path or GATEWAY_CONFIG_PATH
    _atomic_write(p, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)
    return p


def served_servers(path: Path | None = None) -> dict[str, ServedServer]:
    """Servers this machine currently serves through the gateway."""
    out: dict[str, ServedServer] = {}
    servers = _read_config(path).get("servers")
    if not isinstance(servers, dict):
        return out
    for name, entry in servers.items():
        if not isinstance(entry, dict) or not _NAME_RE.match(str(name)):
            continue
        command = entry.get("command")
        if not (
            isinstance(command, list)
            and command
            and all(isinstance(c, str) and c for c in command)
        ):
            continue
        out[name] = ServedServer(
            name=name, command=command, description=str(entry.get("description", ""))
        )
    return out


def serve_add(
    name: str,
    command: list[str] | None = None,
    description: str = "",
    path: Path | None = None,
) -> ServedServer:
    """Serve a built-in server by name, or any stdio MCP command under a name."""
    validate_name(name)
    if not command:
        builtin = BUILTIN_SERVERS.get(name)
        if builtin is None:
            raise ValueError(
                f"{name!r} is not a built-in server; pass the command to serve "
                f"after '--' (built-ins: {', '.join(sorted(BUILTIN_SERVERS))})"
            )
        command = list(builtin["command"])
        description = description or builtin["description"]
    data = _read_config(path)
    servers = data.get("servers") if isinstance(data.get("servers"), dict) else {}
    servers[name] = {"command": list(command), "description": description}
    data["servers"] = servers
    _write_config(data, path)
    return ServedServer(name=name, command=list(command), description=description)


def serve_remove(name: str, path: Path | None = None) -> bool:
    data = _read_config(path)
    servers = data.get("servers") if isinstance(data.get("servers"), dict) else {}
    if name not in servers:
        return False
    del servers[name]
    data["servers"] = servers
    _write_config(data, path)
    return True


def _resolve_command(command: list[str]) -> list[str]:
    """Resolve a bare `openbase-coder` to this installation's binary."""
    if command and command[0] == "openbase-coder":
        return [_openbase_coder_path(), *command[1:]]
    return command


def _openbase_coder_path() -> str:
    exe = Path(sys.argv[0])
    if exe.name == "openbase-coder" and exe.exists():
        return str(exe.resolve())
    candidate = Path(sys.executable).parent / "openbase-coder"
    return str(candidate) if candidate.exists() else "openbase-coder"


def _normalize_frame(text: str) -> str | None:
    """One JSON-RPC message as a single line, or None when it is not JSON.

    MCP stdio is newline-delimited, so a frame with embedded newlines (a
    pretty-printed message) is re-serialized compactly before it reaches the
    process.
    """
    text = text.strip()
    if not text:
        return None
    if "\n" not in text and "\r" not in text:
        return text
    try:
        return json.dumps(json.loads(text), separators=(",", ":"))
    except ValueError:
        return None


class GatewayBridge:
    """Bridges one WebSocket session to one stdio MCP process."""

    def __init__(
        self, server: ServedServer, send_text: Callable[[str], Awaitable[None]]
    ) -> None:
        self.server = server
        self._send_text = send_text
        self.proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the process; raises OSError when it cannot be launched."""
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            # Own process group, so stop() also ends anything it spawned.
            kwargs["start_new_session"] = True
        self.proc = await asyncio.create_subprocess_exec(
            *_resolve_command(self.server.command),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_FRAME_BYTES,
            **kwargs,
        )
        self._stdout_task = asyncio.create_task(self._pump_stdout())
        self._stderr_task = asyncio.create_task(self._pump_stderr())

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            try:
                line = await self.proc.stdout.readline()
            except ValueError:
                logger.warning(
                    "mcp-gateway %s: message over %d bytes; closing the bridge",
                    self.server.name,
                    MAX_FRAME_BYTES,
                )
                return
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                await self._send_text(text)

    async def _pump_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            try:
                line = await self.proc.stderr.readline()
            except ValueError:
                continue
            if not line:
                return
            logger.debug(
                "mcp-gateway %s: %s",
                self.server.name,
                line.decode("utf-8", errors="replace").rstrip(),
            )

    async def to_process(self, text: str) -> bool:
        """Send one message to the process; False when it is gone."""
        line = _normalize_frame(text)
        if line is None:
            return True
        if not self.proc or not self.proc.stdin or self.proc.stdin.is_closing():
            return False
        try:
            self.proc.stdin.write(line.encode("utf-8") + b"\n")
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True

    async def wait_stdout_closed(self) -> None:
        """Return once the process stopped producing output (it exited)."""
        if self._stdout_task is not None:
            await asyncio.wait({self._stdout_task})

    def _signal(self, sig: int, *, include_exited_root: bool = False) -> None:
        proc = self.proc
        if proc is None or (proc.returncode is not None and not include_exited_root):
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass

    async def stop(self) -> None:
        """End the process: close stdin, then SIGTERM, then SIGKILL."""
        proc = self.proc
        if proc is not None and proc.returncode is None:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
            for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_SECONDS)
                    break
                except asyncio.TimeoutError:
                    self._signal(sig)
            else:
                await proc.wait()
        if proc is not None and os.name == "posix":
            # The server itself may have exited while leaving children behind.
            self._signal(
                getattr(signal, "SIGKILL", signal.SIGTERM),
                include_exited_root=True,
            )
        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


# --- requesting side --------------------------------------------------------


def peer_ws_url(base_url: str, name: str, token: str) -> str:
    """ws(s):// URL of a peer's gateway endpoint for a server."""
    from urllib.parse import quote

    if base_url.startswith("https://"):
        root = "wss://" + base_url[len("https://") :]
    elif base_url.startswith("http://"):
        root = "ws://" + base_url[len("http://") :]
    elif base_url.startswith(("ws://", "wss://")):
        root = base_url
    else:
        root = "ws://" + base_url
    path = GATEWAY_WS_PATH.format(name=quote(name, safe=""))
    return f"{root.rstrip('/')}/{path}?token={quote(token, safe='')}"


def _start_stdin_reader(
    loop: asyncio.AbstractEventLoop, fd: int
) -> asyncio.Queue[bytes | None]:
    """Lines read from a file descriptor, delivered on the event loop.

    A daemon thread blocked in ``os.read`` holds no Python-level lock, so the
    process can exit while the MCP client still has our stdin open (the peer
    went away). An executor thread or ``sys.stdin.buffer.readline`` would keep
    the interpreter waiting on that read at shutdown.
    """
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def put(item: bytes | None) -> None:
        with contextlib.suppress(RuntimeError):  # loop already closed
            loop.call_soon_threadsafe(queue.put_nowait, item)

    def run() -> None:
        pending = b""
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                if pending.strip():
                    put(pending)
                put(None)
                return
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                put(line)

    threading.Thread(target=run, name="mcp-gateway-stdin", daemon=True).start()
    return queue


def _close_detail(ws: Any) -> str:
    code = getattr(ws, "close_code", None)
    reason = getattr(ws, "close_reason", None) or ""
    if code is None:
        return "connection lost"
    return f"{code} {reason}".strip()


async def relay_stdio(
    url: str,
    *,
    stdin_fd: int | None = None,
    stdout: BinaryIO | None = None,
    stderr: Any = None,
    open_timeout: float = 5.0,
) -> int:
    """Relay this process's stdio (an MCP client) to a gateway WebSocket.

    Returns a process exit code: 0 when the client closed stdin, 69
    (EX_UNAVAILABLE) when the peer could not be reached or closed the bridge.
    Nothing but relayed JSON-RPC messages is ever written to ``stdout``.
    """
    import websockets
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    loop = asyncio.get_running_loop()
    stdin_fd = sys.stdin.fileno() if stdin_fd is None else stdin_fd
    stdout = stdout or sys.stdout.buffer
    stderr = stderr or sys.stderr

    def report(message: str) -> None:
        with contextlib.suppress(Exception):
            stderr.write(f"mcp-gateway: {message}\n")
            stderr.flush()

    try:
        ws = await websockets.connect(
            url,
            max_size=MAX_FRAME_BYTES,
            open_timeout=open_timeout,
            ping_interval=20,
            # Tailnet peers are reached directly, never through an HTTP proxy.
            proxy=None,
        )
    except InvalidStatus as exc:
        status = exc.response.status_code
        hint = " (not authorized: sign in on both machines)" if status == 403 else ""
        report(f"peer refused the connection: HTTP {status}{hint}")
        return EXIT_UNAVAILABLE
    except Exception as exc:  # noqa: BLE001 — any failure means "not available"
        report(f"peer unavailable: {exc or type(exc).__name__}")
        return EXIT_UNAVAILABLE

    lines = _start_stdin_reader(loop, stdin_fd)

    async def client_to_peer() -> int:
        while True:
            line = await lines.get()
            if line is None:
                return EXIT_OK
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                await ws.send(text)
            except ConnectionClosed:
                report(f"peer closed the bridge ({_close_detail(ws)})")
                return EXIT_UNAVAILABLE

    async def peer_to_client() -> int:
        try:
            async for message in ws:
                data = (
                    message if isinstance(message, bytes) else message.encode("utf-8")
                )
                stdout.write(data.rstrip(b"\r\n") + b"\n")
                stdout.flush()
        except ConnectionClosed:
            pass
        report(f"peer closed the bridge ({_close_detail(ws)})")
        return EXIT_UNAVAILABLE

    tasks = [
        asyncio.create_task(client_to_peer()),
        asyncio.create_task(peer_to_client()),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    with contextlib.suppress(Exception):
        await ws.close()
    return next(iter(done)).result()


# --- offering servers to this machine's agents --------------------------------


def offered_name(name: str) -> str:
    return f"{name}{OFFER_SUFFIX}"


def _shim_entry(name: str, peer: str) -> tuple[str, list[str]]:
    return _openbase_coder_path(), ["mcp-gateway", "connect", name, "--peer", peer]


def _is_gateway_entry(entry: Any) -> bool:
    if not isinstance(entry, dict) and not hasattr(entry, "get"):
        return False
    args = entry.get("args") or []
    try:
        args = [str(a) for a in args]
    except TypeError:
        return False
    return args[:2] == ["mcp-gateway", "connect"]


def _codex_paths(codex_paths: list[Path] | None) -> list[Path]:
    if codex_paths is not None:
        return codex_paths
    return [CODEX_PROFILE_PATH, CLOUD_CODEX_PROFILE_PATH]


def _read_claude_profile(path: Path) -> dict[str, Any]:
    """The Claude profile's JSON; raises ValueError instead of discarding it."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON; fix it first ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object; fix it first")
    return data


def offer(
    name: str,
    peer: str,
    *,
    claude_path: Path | None = None,
    codex_paths: list[Path] | None = None,
) -> list[Path]:
    """List `<name>-laptop` in this machine's agent profiles.

    This only makes the server available; the agent decides whether and when
    to call it. Codex profiles are edited only when they already exist.
    Raises ValueError when a profile already holds a different, non-gateway
    server under the same key.
    """
    validate_name(name)
    if not peer:
        raise ValueError("a peer device name is required")
    command, args = _shim_entry(name, peer)
    key = offered_name(name)
    cp = claude_path or CLAUDE_PROFILE_MCP_PATH
    codex = [p for p in _codex_paths(codex_paths) if p.exists()]

    # Read and check everything before writing anything.
    data = _read_claude_profile(cp)
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
    if key in servers and not _is_gateway_entry(servers[key]):
        raise ValueError(f"{cp} already has a different MCP server named {key!r}")
    codex_docs = []
    for path in codex:
        doc = _parse_toml(path)
        current = (doc.get("mcp_servers") or {}).get(key)
        if current is not None and not _is_gateway_entry(current):
            raise ValueError(f"{path} already has a different MCP server named {key!r}")
        codex_docs.append((path, doc))

    changed: list[Path] = []
    entry = {"type": "stdio", "command": command, "args": args}
    if servers.get(key) != entry:
        servers[key] = entry
        data["mcpServers"] = servers
        _atomic_write(cp, json.dumps(data, indent=2, sort_keys=True) + "\n")
        changed.append(cp)
    for path, doc in codex_docs:
        if _set_codex_server(path, doc, key, command, args):
            changed.append(path)
    return changed


def withdraw(
    name: str, *, claude_path: Path | None = None, codex_paths: list[Path] | None = None
) -> list[Path]:
    """Remove `<name>-laptop` from this machine's agent profiles.

    Only gateway entries are removed; a user's own server that happens to
    share the name is left alone.
    """
    key = offered_name(name)
    changed: list[Path] = []
    cp = claude_path or CLAUDE_PROFILE_MCP_PATH
    data = _read_claude_profile(cp)
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
    if key in servers and _is_gateway_entry(servers[key]):
        del servers[key]
        data["mcpServers"] = servers
        _atomic_write(cp, json.dumps(data, indent=2, sort_keys=True) + "\n")
        changed.append(cp)
    for path in _codex_paths(codex_paths):
        if path.exists() and _remove_codex_server(path, key):
            changed.append(path)
    return changed


@dataclass(frozen=True)
class OfferedServer:
    key: str
    name: str
    peer: str
    profiles: tuple[Path, ...]


def _offer_from_args(key: str, args: list[str]) -> tuple[str, str]:
    name = args[2] if len(args) > 2 else key.removesuffix(OFFER_SUFFIX)
    peer = ""
    if "--peer" in args:
        index = args.index("--peer")
        if index + 1 < len(args):
            peer = args[index + 1]
    return name, peer


def offered(
    *, claude_path: Path | None = None, codex_paths: list[Path] | None = None
) -> dict[str, OfferedServer]:
    """Gateway servers offered to this machine's agents, by config key."""
    found: dict[str, tuple[str, str, list[Path]]] = {}

    def note(key: str, entry: Any, path: Path) -> None:
        if not key.endswith(OFFER_SUFFIX) or not _is_gateway_entry(entry):
            return
        args = [str(a) for a in entry.get("args") or []]
        name, peer = _offer_from_args(key, args)
        found.setdefault(key, (name, peer, []))[2].append(path)

    cp = claude_path or CLAUDE_PROFILE_MCP_PATH
    try:
        data = _read_claude_profile(cp)
    except ValueError:
        data = {}
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
    for key, entry in servers.items():
        note(key, entry, cp)
    for path in _codex_paths(codex_paths):
        if not path.exists():
            continue
        try:
            doc = _parse_toml(path)
        except ValueError:
            continue
        for key, entry in (doc.get("mcp_servers") or {}).items():
            note(key, entry, path)
    return {
        key: OfferedServer(key=key, name=name, peer=peer, profiles=tuple(paths))
        for key, (name, peer, paths) in sorted(found.items())
    }


def _parse_toml(path: Path):
    import tomlkit
    from tomlkit.exceptions import TOMLKitError

    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except TOMLKitError as exc:
        raise ValueError(f"{path} is not valid TOML; fix it first ({exc})") from exc


def _set_codex_server(path: Path, doc, key: str, command: str, args: list[str]) -> bool:
    import tomlkit

    servers = doc.get("mcp_servers")
    if servers is None:
        servers = tomlkit.table(is_super_table=True)
        doc["mcp_servers"] = servers
    current = servers.get(key)
    if (
        current is not None
        and current.get("command") == command
        and list(current.get("args", [])) == args
    ):
        return False
    table = tomlkit.table()
    table["command"] = command
    table["args"] = args
    if key in servers:
        del servers[key]
    servers[key] = table
    _atomic_write(path, tomlkit.dumps(doc))
    return True


def _remove_codex_server(path: Path, key: str) -> bool:
    import tomlkit

    doc = _parse_toml(path)
    servers = doc.get("mcp_servers")
    if servers is None or key not in servers or not _is_gateway_entry(servers[key]):
        return False
    del servers[key]
    _atomic_write(path, tomlkit.dumps(doc))
    return True
