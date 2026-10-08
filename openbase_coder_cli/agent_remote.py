"""Run ``openbase-coder codex|claude`` on the Openbase Sync hub from an edge.

When this computer is a paired *edge* (a laptop), the folder you are in is
synced, and the hub's Openbase runtime is reachable over the Openbase VPN,
the session runs **on the hub** — where it keeps running when the laptop
sleeps — and this terminal attaches to it. Otherwise it runs locally.

Transport is the hub runtime's agent-terminal WebSocket
(``ws/agent-terminals/``), the sibling of the thread terminal: no SSH. The
hub starts the agent in a PTY with its own Openbase profile and keeps the
PTY alive across reconnects. Frames:

- binary frames carry raw terminal bytes both ways;
- text frames are JSON control messages. Client: ``start`` (agent, cwd,
  args, cols, rows), ``resize``, ``restart``. Server: ``ready`` (session id,
  notices), ``exit`` (code), ``error`` (message).

Synced folders have the same home-relative path on both computers, so the
cwd is sent home-relative (``~/Projects/app``) and expanded by the hub.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import signal
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

LOCAL = "local"
REMOTE = "remote"
HUB_URL_ENV = "OPENBASE_HUB_URL"
HUB_PROBE_TIMEOUT_SECONDS = 2.0
CONNECT_TIMEOUT_SECONDS = 10.0
READY_TIMEOUT_SECONDS = 30.0
RECONNECT_ATTEMPTS = 5


@dataclasses.dataclass(frozen=True)
class SyncFacts:
    """What the local Openbase Sync configuration says about this computer."""

    configured: bool
    role: str = ""
    hub_host: str = ""
    roots: tuple[dict[str, Any], ...] = ()


@dataclasses.dataclass(frozen=True)
class ModeDecision:
    mode: str
    reason: str | None = None  # one line shown to the user, if any
    hub_url: str | None = None


class RemoteUnavailableError(RuntimeError):
    """The hub could not start the session (message meant for the user)."""


# --- sync facts ------------------------------------------------------------


def _host_from_peer(peer: str) -> str:
    peer = peer.strip()
    if peer.startswith("["):
        return peer[1 : peer.find("]")] if "]" in peer else peer.strip("[]")
    if peer.count(":") == 1:
        return peer.rsplit(":", 1)[0]
    return peer


def read_sync_facts(config_path: Path | None = None) -> SyncFacts:
    """Role, hub host and roots (with ignores) from the sync config."""
    from openbase_coder_cli.sync_daemon import SYNC_DAEMON_CONFIG_PATH

    path = config_path or SYNC_DAEMON_CONFIG_PATH
    if not path.is_file():
        return SyncFacts(configured=False)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return SyncFacts(configured=False)
    roots = []
    for raw in data.get("roots") or []:
        if isinstance(raw, dict) and isinstance(raw.get("path"), str):
            ignore = raw.get("ignore")
            roots.append(
                {
                    "path": raw["path"],
                    "ignore": [str(x) for x in ignore]
                    if isinstance(ignore, list)
                    else [],
                }
            )
    role = data.get("role") if isinstance(data.get("role"), str) else ""
    peer = data.get("peer_hot") if isinstance(data.get("peer_hot"), str) else ""
    return SyncFacts(
        configured=True,
        role=role,
        hub_host=_host_from_peer(peer) if peer else "",
        roots=tuple(roots),
    )


def _expand(path: str, home: Path) -> Path:
    if path == "~":
        return home
    if path.startswith("~/"):
        return home / path[2:]
    return Path(path)


def _ignored(rel_parts: tuple[str, ...], patterns: Sequence[str]) -> bool:
    """Whether a root-relative path falls under one of the root's ignores.

    Anchored patterns (``/a/b``) match that path and everything below it;
    bare names (``.generated``) match any path component.
    """
    for pattern in patterns:
        pattern = pattern.strip()
        if not pattern:
            continue
        if pattern.startswith("/"):
            parts = tuple(p for p in pattern.strip("/").split("/") if p)
            if parts and rel_parts[: len(parts)] == parts:
                return True
        elif "/" not in pattern.strip("/") and pattern.strip("/") in rel_parts:
            return True
    return False


def synced_root_for(
    cwd: Path, roots: Sequence[Mapping[str, Any]], home: Path
) -> Path | None:
    """The synced root containing ``cwd`` (None when not synced or ignored)."""
    target = Path(os.path.realpath(cwd))
    for root in roots:
        base = Path(os.path.realpath(_expand(str(root["path"]), home)))
        if target != base and base not in target.parents:
            continue
        rel = target.relative_to(base).parts
        if _ignored(rel, root.get("ignore") or ()):
            continue
        return base
    return None


def home_relative(path: Path, home: Path) -> str:
    """``~/...`` under the home directory (same on hub and edge), else absolute."""
    resolved = Path(os.path.realpath(path))
    try:
        rel = resolved.relative_to(Path(os.path.realpath(home)))
    except ValueError:
        return str(resolved)
    return "~" if not rel.parts else "~/" + rel.as_posix()


def hub_base_url(host: str, environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    override = environ.get(HUB_URL_ENV, "").strip().rstrip("/")
    if override:
        return override
    from openbase_coder_cli.services.tailnet_devices import (
        OPENBASE_CODER_TAILNET_PORT,
        _url_host_literal,
    )

    return f"http://{_url_host_literal(host)}:{OPENBASE_CODER_TAILNET_PORT}"


def probe_hub(base_url: str, timeout: float = HUB_PROBE_TIMEOUT_SECONDS) -> bool:
    import httpx

    try:
        response = httpx.get(f"{base_url}/api/health/", timeout=timeout)
    except httpx.HTTPError:
        return False
    return response.is_success


# --- mode selection --------------------------------------------------------


def select_mode(
    *,
    force: str | None,
    facts: SyncFacts,
    cwd: Path,
    home: Path,
    interactive: bool,
    probe: Callable[[str], bool] = probe_hub,
    environ: Mapping[str, str] | None = None,
    platform: str = sys.platform,
) -> ModeDecision:
    """Where to run: locally, or on the hub this edge is paired with.

    Unpaired computers and the hub itself run locally without a word. An edge
    explains in one line why it falls back to local. ``force`` is ``local``,
    ``remote`` or None; a forced remote that cannot be honored raises
    ``RemoteUnavailableError`` instead of silently running locally.
    """
    if force == LOCAL:
        return ModeDecision(LOCAL)

    def fallback(reason: str) -> ModeDecision:
        if force == REMOTE:
            raise RemoteUnavailableError(reason)
        return ModeDecision(LOCAL, reason)

    if not interactive:
        if force == REMOTE:
            raise RemoteUnavailableError(
                "Only interactive sessions can run on the hub."
            )
        return ModeDecision(LOCAL)
    if not facts.configured or facts.role != "edge":
        if force == REMOTE:
            raise RemoteUnavailableError(
                "This computer is not an Openbase Sync edge paired with a hub."
            )
        return ModeDecision(LOCAL)
    if platform == "win32":
        return fallback("Remote sessions need a POSIX terminal; running locally.")
    if not facts.hub_host:
        return fallback("The sync config names no hub; running locally.")
    if synced_root_for(cwd, facts.roots, home) is None:
        return fallback(
            f"{home_relative(cwd, home)} is not in a synced folder; running locally."
        )
    base_url = hub_base_url(facts.hub_host, environ)
    if not probe(base_url):
        return fallback(f"The hub ({facts.hub_host}) is unreachable; running locally.")
    return ModeDecision(REMOTE, hub_url=base_url)


# --- framing ---------------------------------------------------------------


def start_message(
    agent: str, cwd: str, args: Sequence[str], cols: int, rows: int
) -> str:
    return json.dumps(
        {
            "type": "start",
            "agent": agent,
            "cwd": cwd,
            "args": list(args),
            "cols": cols,
            "rows": rows,
        }
    )


def resize_message(cols: int, rows: int) -> str:
    return json.dumps({"type": "resize", "cols": cols, "rows": rows})


def decode_frame(frame: str | bytes) -> tuple[str, Any]:
    """``("output", bytes)`` or ``(control type, data)`` for a server frame."""
    if isinstance(frame, (bytes, bytearray, memoryview)):
        return "output", bytes(frame)
    try:
        message = json.loads(frame)
    except json.JSONDecodeError:
        return "unknown", frame
    if not isinstance(message, dict):
        return "unknown", message
    kind = str(message.get("type") or "unknown")
    data = message.get("data")
    return kind, data if isinstance(data, dict) else {}


def socket_url(base_url: str, path: str, token: str, **params: Any) -> str:
    if base_url.startswith("https://"):
        root = "wss://" + base_url[len("https://") :]
    elif base_url.startswith("http://"):
        root = "ws://" + base_url[len("http://") :]
    else:
        root = base_url
    query = urlencode({"token": token, **{k: v for k, v in params.items()}})
    return f"{root}{path}?{query}"


# --- terminal client -------------------------------------------------------


def terminal_size(fd: int) -> tuple[int, int]:
    try:
        size = os.get_terminal_size(fd)
    except OSError:
        return 120, 32
    return size.columns, size.lines


@contextlib.contextmanager
def raw_terminal(fd: int):
    """Raw mode for the session; always restored, even on errors."""
    if not os.isatty(fd):
        yield
        return
    import termios
    import tty

    saved = termios.tcgetattr(fd)
    try:
        # Raw mode clears ISIG, so ^C/^Z reach the remote TUI as bytes
        # instead of signalling this client.
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


class RemoteSession:
    """Pumps one hub PTY session to and from this terminal."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        agent: str,
        cwd: str,
        args: Sequence[str],
        stdin_fd: int = 0,
        stdout_fd: int = 1,
        notice: Callable[[str], None] | None = None,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self.token = token
        self.agent = agent
        self.cwd = cwd
        self.args = list(args)
        self.stdin_fd = stdin_fd
        self.stdout_fd = stdout_fd
        self.notice = notice or (lambda line: print(line, file=sys.stderr))
        self._connect = connect
        self.session_id: str | None = None
        self.exit_code: int | None = None
        self._ws: Any = None
        self._stdin_closed = False

    def _open(self, url: str):
        if self._connect is not None:
            return self._connect(url)
        from websockets.asyncio.client import connect

        return connect(
            url,
            open_timeout=CONNECT_TIMEOUT_SECONDS,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        )

    async def start(self) -> list[str]:
        """Open the socket and start the session; returns the hub's notices."""
        cols, rows = terminal_size(self.stdout_fd)
        url = socket_url(
            self.base_url, "/ws/agent-terminals/", self.token, cols=cols, rows=rows
        )
        try:
            self._ws = await self._open(url)
        except (OSError, asyncio.TimeoutError) as exc:
            raise RemoteUnavailableError(f"Could not reach the hub: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - websockets handshake errors
            raise RemoteUnavailableError(_handshake_message(exc)) from exc
        await self._ws.send(start_message(self.agent, self.cwd, self.args, cols, rows))
        try:
            ready = await asyncio.wait_for(self._await_ready(), READY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            await self._close()
            raise RemoteUnavailableError(
                "The hub did not start the session in time."
            ) from exc
        self.session_id = str(ready.get("id") or "")
        notices = ready.get("notices")
        return [str(n) for n in notices] if isinstance(notices, list) else []

    async def _await_ready(self) -> dict[str, Any]:
        while True:
            try:
                frame = await self._ws.recv()
            except Exception as exc:  # noqa: BLE001 - closed before ready
                raise RemoteUnavailableError(
                    _close_message(exc, "The hub closed the session before it started.")
                ) from exc
            kind, data = decode_frame(frame)
            if kind == "ready":
                return data
            if kind == "error":
                await self._close()
                raise RemoteUnavailableError(
                    str(data.get("message") or "The hub refused the session.")
                )
            if kind == "output":
                # Early PTY output is replayed after ready; nothing to do.
                continue

    async def run(self) -> int:
        """Pump until the remote process exits; returns its exit code."""
        loop = asyncio.get_running_loop()
        input_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        resized = asyncio.Event()

        def on_stdin() -> None:
            try:
                data = os.read(self.stdin_fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if not data:
                loop.remove_reader(self.stdin_fd)
                self._stdin_closed = True
                return
            input_queue.put_nowait(data)

        loop.add_reader(self.stdin_fd, on_stdin)
        winch_installed = False
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(signal.SIGWINCH, resized.set)
            winch_installed = True
        try:
            attempts = 0
            while True:
                outcome = await self._pump(input_queue, resized)
                if outcome == "exit":
                    return self.exit_code if self.exit_code is not None else 0
                if outcome == "error":
                    return 1
                # Connection lost: reattach to the same PTY on the hub.
                attempts += 1
                if attempts > RECONNECT_ATTEMPTS or not self.session_id:
                    self._write_status(
                        "Lost the connection to the hub. The session keeps "
                        "running there for a while; reopen it from the "
                        "Openbase app."
                    )
                    return 1
                await asyncio.sleep(min(2 ** (attempts - 1), 10))
                if await self._reattach():
                    attempts = 0
        finally:
            with contextlib.suppress(Exception):
                loop.remove_reader(self.stdin_fd)
            if winch_installed:
                with contextlib.suppress(Exception):
                    loop.remove_signal_handler(signal.SIGWINCH)
            await self._close()

    async def _reattach(self) -> bool:
        cols, rows = terminal_size(self.stdout_fd)
        url = socket_url(
            self.base_url,
            f"/ws/agent-terminals/{self.session_id}/",
            self.token,
            cols=cols,
            rows=rows,
            replay=0,
        )
        try:
            self._ws = await self._open(url)
            ready = await asyncio.wait_for(self._await_ready(), READY_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - retried by the caller
            return False
        return bool(ready)

    async def _pump(
        self, input_queue: asyncio.Queue[bytes | None], resized: asyncio.Event
    ) -> str:
        ws = self._ws

        async def send_input() -> None:
            while True:
                data = await input_queue.get()
                if data is None:
                    return
                await ws.send(data)

        async def send_resizes() -> None:
            while True:
                await resized.wait()
                resized.clear()
                cols, rows = terminal_size(self.stdout_fd)
                await ws.send(resize_message(cols, rows))

        async def receive() -> str:
            while True:
                try:
                    frame = await ws.recv()
                except Exception:  # noqa: BLE001 - connection lost
                    return "lost"
                kind, data = decode_frame(frame)
                if kind == "output":
                    _write_all(self.stdout_fd, data)
                elif kind == "exit":
                    code = data.get("code")
                    self.exit_code = code if isinstance(code, int) else 0
                    return "exit"
                elif kind == "error":
                    self._write_status(str(data.get("message") or "Session error."))
                    return "error"

        tasks = [
            asyncio.ensure_future(send_input()),
            asyncio.ensure_future(send_resizes()),
        ]
        try:
            return await receive()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task

    def _write_status(self, line: str) -> None:
        _write_all(self.stdout_fd, f"\r\n[openbase] {line}\r\n".encode())

    async def _close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            import select

            select.select([], [fd], [], 1.0)
            continue
        view = view[written:]


def _handshake_message(exc: Exception) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return "The hub rejected this computer's Openbase login."
    if status in (404, 500):
        # An unknown WebSocket route: the hub's runtime predates remote
        # sessions.
        return (
            f"The hub could not open a remote session (HTTP {status}); "
            "its Openbase may need an update."
        )
    return f"Could not open a session on the hub: {exc}."


def _close_message(exc: Exception, default: str) -> str:
    code = getattr(getattr(exc, "rcvd", None), "code", None)
    if code == 4001:
        return "The hub rejected this computer's Openbase login."
    return default


def run_remote(
    *,
    base_url: str,
    token: str,
    agent: str,
    cwd: str,
    args: Sequence[str],
    on_ready: Callable[[list[str]], None],
) -> int:
    """Start the session on the hub and attach this terminal to it.

    Raises ``RemoteUnavailableError`` if the hub refuses or cannot be reached
    before the session starts (callers may then run locally).
    """

    async def main() -> int:
        session = RemoteSession(
            base_url=base_url, token=token, agent=agent, cwd=cwd, args=args
        )
        notices = await session.start()
        on_ready(notices)
        with raw_terminal(session.stdin_fd):
            return await session.run()

    return asyncio.run(main())
