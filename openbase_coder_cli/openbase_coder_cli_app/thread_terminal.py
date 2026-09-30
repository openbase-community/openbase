"""Native backend TUIs (Codex / Claude Code) attached to a thread, over a PTY.

The console's opt-in Terminal tab renders the real backend CLI for a thread
instead of Openbase's own chat view. Each thread gets at most one PTY-backed
process, owned by this (single-worker) server process and keyed by thread id:
viewers attach and detach over WebSocket, and a reattaching viewer is sent the
recent output so switching tabs does not restart the TUI. With no viewer left
the process is kept for an idle grace period, then terminated.

The conversation is auto-loaded:

- Codex threads run ``codex resume <thread id> --remote <endpoint>`` against
  the same managed app-server Openbase drives, so the TUI is a second client of
  the live thread (turns started from either side show up in both).
- Claude Code threads run ``claude --resume <backend session id>`` in the
  thread's directory with the runtime's own Claude environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import signal
import struct
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from openbase_coder_cli.backend_binaries import find_backend_binary
from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)

logger = logging.getLogger(__name__)

CODEX_TERMINAL_BACKENDS = frozenset({CODEX_BACKEND, OPENBASE_CLOUD_CODEX_BACKEND})
CLAUDE_TERMINAL_BACKENDS = frozenset({CLAUDE_CODE_BACKEND, OPENBASE_CLOUD_BACKEND})

# Output kept for replay to a (re)attaching viewer. Large enough to hold a
# full-screen TUI's recent redraws; trimmed at line boundaries.
REPLAY_BUFFER_BYTES = 2 * 1024 * 1024
IDLE_GRACE_SECONDS = 15 * 60
MAX_TERMINAL_SESSIONS = 12
DEFAULT_COLS = 120
DEFAULT_ROWS = 32
_READ_CHUNK = 64 * 1024


class TerminalUnavailableError(RuntimeError):
    """The thread cannot be opened in a native terminal (with a user message)."""


@dataclasses.dataclass(frozen=True)
class TerminalLaunch:
    backend: str
    argv: list[str]
    cwd: str
    env: dict[str, str]

    @property
    def target(self) -> str:
        return "codex" if self.backend in CODEX_TERMINAL_BACKENDS else "claude_code"


def terminal_supported() -> bool:
    return sys.platform != "win32"


# Identity markers of the agent session that launched this runtime (e.g. a
# runtime started from a Claude Code or Codex shell). Inherited, they make the
# TUI believe it is a nested child session: Claude Code then stops saving the
# transcript, so turns typed in the terminal would never reach the thread.
_INHERITED_SESSION_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_EFFORT",
    "CLAUDE_PID",
    "CODEX_THREAD_ID",
    "CODEX_SANDBOX",
    "CODEX_SANDBOX_NETWORK_DISABLED",
)


def _terminal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for name in _INHERITED_SESSION_ENV:
        env.pop(name, None)
    env.update(
        {
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "TERM_PROGRAM": "openbase",
            "FORCE_COLOR": "1",
        }
    )
    env.pop("NO_COLOR", None)
    # A service-launched runtime may carry a non-UTF-8 locale; TUIs render
    # box-drawing and emoji glyphs.
    if "UTF-8" not in env.get("LANG", "").upper():
        env["LANG"] = "en_US.UTF-8"
    if extra:
        env.update(extra)
    return env


def _launch_cwd(directory: str | None) -> str:
    if directory:
        path = Path(directory).expanduser()
        if path.is_dir():
            return str(path)
    return str(Path.home())


def _codex_endpoint() -> str:
    from openbase_coder_cli.codex_control_plane import managed_codex_app_server_endpoint

    return managed_codex_app_server_endpoint().value


def _claude_backend_env(backend: str) -> dict[str, str]:
    if backend != OPENBASE_CLOUD_BACKEND:
        return {}
    from super_agents.claude_options import openbase_cloud_claude_env

    return openbase_cloud_claude_env(backend)


def resolve_terminal_launch(
    *,
    thread_id: str,
    backend: str | None,
    backend_session_id: str | None,
    directory: str | None,
    find_binary: Callable[[str], Path | None] = find_backend_binary,
    codex_endpoint: Callable[[], str] = _codex_endpoint,
    claude_env: Callable[[str], dict[str, str]] = _claude_backend_env,
) -> TerminalLaunch:
    """The native CLI command that reopens ``thread_id`` with its conversation."""
    normalized = (backend or "").strip() or None
    if normalized is None:
        # Legacy payloads omit the backend: a Claude session always carries its
        # backend session id, a Codex thread never does.
        normalized = CLAUDE_CODE_BACKEND if backend_session_id else CODEX_BACKEND
    cwd = _launch_cwd(directory)

    if normalized in CODEX_TERMINAL_BACKENDS:
        binary = find_binary("codex")
        if binary is None:
            raise TerminalUnavailableError(
                "Codex CLI is not installed on this computer."
            )
        argv = [
            str(binary),
            "resume",
            thread_id,
            "--remote",
            codex_endpoint(),
        ]
        return TerminalLaunch(normalized, argv, cwd, _terminal_env())

    if normalized in CLAUDE_TERMINAL_BACKENDS:
        if not backend_session_id:
            raise TerminalUnavailableError(
                "This Claude Code thread has no saved session yet. "
                "Start a turn first, then open the terminal."
            )
        binary = find_binary("claude")
        if binary is None:
            raise TerminalUnavailableError(
                "Claude Code CLI is not installed on this computer."
            )
        argv = [str(binary), "--resume", backend_session_id]
        return TerminalLaunch(
            normalized, argv, cwd, _terminal_env(claude_env(normalized))
        )

    raise TerminalUnavailableError(
        f"Threads on the {normalized} backend cannot be opened in a terminal."
    )


def _acquire_controlling_tty() -> None:  # pragma: no cover - runs in the child
    import fcntl
    import termios

    # start_new_session already made the child a session leader; claim the
    # PTY slave (its stdin) so resizes deliver SIGWINCH and ^C/^Z work.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


Listener = Callable[[str, object], None]


class TerminalSession:
    """One PTY-backed TUI process and the viewers attached to it."""

    def __init__(self, key: str, launch: TerminalLaunch) -> None:
        self.key = key
        self.launch = launch
        self.cols = DEFAULT_COLS
        self.rows = DEFAULT_ROWS
        self.exit_code: int | None = None
        self._buffer = bytearray()
        self._listeners: set[Listener] = set()
        self._master_fd: int | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._pending_input = bytearray()
        self._writer_registered = False
        self._idle_handle: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_closed: Callable[[TerminalSession], None] | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self.exit_code is None

    @property
    def viewer_count(self) -> int:
        return len(self._listeners)

    def start(
        self,
        cols: int,
        rows: int,
        *,
        on_closed: Callable[[TerminalSession], None] | None = None,
    ) -> None:
        import pty

        self._loop = asyncio.get_running_loop()
        self._on_closed = on_closed
        self.cols, self.rows = _clamp_size(cols, rows)
        master_fd, slave_fd = pty.openpty()
        _set_winsize(slave_fd, self.cols, self.rows)
        try:
            self._process = subprocess.Popen(  # noqa: S603 - fixed argv
                self.launch.argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=self.launch.cwd,
                env=self.launch.env,
                start_new_session=True,
                preexec_fn=_acquire_controlling_tty,  # noqa: PLW1509
                close_fds=True,
            )
        except BaseException:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        os.set_blocking(master_fd, False)
        self._master_fd = master_fd
        self._loop.add_reader(master_fd, self._on_readable)
        logger.info(
            "thread_terminal started key=%s backend=%s pid=%s",
            self.key,
            self.launch.backend,
            self._process.pid,
        )

    # -- viewers ---------------------------------------------------------

    def attach(self, listener: Listener) -> bytes:
        """Register a viewer; returns the replay buffer to send it first."""
        self._listeners.add(listener)
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        return bytes(self._buffer)

    def detach(self, listener: Listener) -> None:
        self._listeners.discard(listener)
        if self._listeners or self._loop is None:
            return
        if not self.running:
            self.close()
            return
        self._idle_handle = self._loop.call_later(IDLE_GRACE_SECONDS, self.close)

    def _emit(self, kind: str, payload: object) -> None:
        for listener in list(self._listeners):
            try:
                listener(kind, payload)
            except Exception:  # noqa: BLE001 - one bad viewer must not stall the PTY
                logger.exception("thread_terminal listener failed key=%s", self.key)

    # -- I/O -------------------------------------------------------------

    def _on_readable(self) -> None:
        if self._master_fd is None:
            return
        try:
            data = os.read(self._master_fd, _READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            # EIO: every slave fd is closed, i.e. the process exited.
            data = b""
        if not data:
            self._handle_exit()
            return
        self._buffer.extend(data)
        if len(self._buffer) > REPLAY_BUFFER_BYTES:
            overflow = len(self._buffer) - REPLAY_BUFFER_BYTES
            newline = self._buffer.find(b"\n", overflow)
            del self._buffer[: newline + 1 if newline != -1 else overflow]
        self._emit("output", data)

    def write(self, data: bytes) -> None:
        if not self.running or self._master_fd is None or not data:
            return
        self._pending_input.extend(data)
        self._flush_input()

    def _flush_input(self) -> None:
        if self._master_fd is None:
            return
        while self._pending_input:
            try:
                written = os.write(self._master_fd, self._pending_input)
            except BlockingIOError:
                break
            except OSError:
                self._pending_input.clear()
                break
            del self._pending_input[:written]
        if self._pending_input and not self._writer_registered and self._loop:
            self._loop.add_writer(self._master_fd, self._flush_input)
            self._writer_registered = True
        elif not self._pending_input and self._writer_registered and self._loop:
            self._loop.remove_writer(self._master_fd)
            self._writer_registered = False

    def resize(self, cols: int, rows: int, *, force_redraw: bool = False) -> None:
        cols, rows = _clamp_size(cols, rows)
        changed = (cols, rows) != (self.cols, self.rows)
        self.cols, self.rows = cols, rows
        if self._master_fd is None or not self.running:
            return
        if changed:
            _set_winsize(self._master_fd, cols, rows)
        if changed or force_redraw:
            # The kernel signals the foreground group on a real size change;
            # an explicit SIGWINCH also makes a reattached TUI repaint.
            self._signal(signal.SIGWINCH)

    def _signal(self, signum: int) -> None:
        if self._process is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._process.pid, signum)

    # -- lifecycle -------------------------------------------------------

    def _handle_exit(self) -> None:
        self._close_master()
        process = self._process
        if process is None or self._loop is None:
            self._finish_exit(-1)
            return
        code = process.poll()
        if code is not None:
            self._finish_exit(code)
            return
        # The PTY closed a moment before the process finished exiting; reap
        # it off the event loop.
        future = self._loop.run_in_executor(None, _reap, process)
        future.add_done_callback(
            lambda _future: self._finish_exit(
                process.returncode if process.returncode is not None else -1
            )
        )

    def _finish_exit(self, code: int) -> None:
        if self.exit_code is not None:
            return
        self.exit_code = code
        logger.info("thread_terminal exited key=%s code=%s", self.key, code)
        self._emit("exit", code)
        if not self._listeners:
            self.close()

    def _close_master(self) -> None:
        if self._master_fd is None:
            return
        if self._loop is not None:
            self._loop.remove_reader(self._master_fd)
            if self._writer_registered:
                self._loop.remove_writer(self._master_fd)
                self._writer_registered = False
        with contextlib.suppress(OSError):
            os.close(self._master_fd)
        self._master_fd = None

    def close(self) -> None:
        """Terminate the process (if still running) and forget the session."""
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        if self.running:
            self._signal(signal.SIGHUP)
            self._signal(signal.SIGTERM)
            process = self._process
            self._close_master()
            if process is not None and self._loop is not None:
                self._loop.run_in_executor(None, _reap, process)
            self.exit_code = -signal.SIGTERM
            self._emit("exit", self.exit_code)
        else:
            self._close_master()
        if self._on_closed is not None:
            callback, self._on_closed = self._on_closed, None
            callback(self)


def _reap(process: subprocess.Popen[bytes]) -> int:
    try:
        return process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        return process.wait()


def _clamp_size(cols: int, rows: int) -> tuple[int, int]:
    return max(20, min(int(cols), 500)), max(5, min(int(rows), 200))


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    import fcntl
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class TerminalRegistry:
    """Per-process map of thread id → live terminal session."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}

    def get(self, key: str) -> TerminalSession | None:
        session = self._sessions.get(key)
        if session is not None and not session.running and not session.viewer_count:
            session.close()
            return None
        return session

    def open(
        self, key: str, launch: TerminalLaunch, cols: int, rows: int
    ) -> TerminalSession:
        existing = self._sessions.get(key)
        if existing is not None:
            existing.close()
        if len(self._sessions) >= MAX_TERMINAL_SESSIONS:
            self._evict_one_idle()
        if len(self._sessions) >= MAX_TERMINAL_SESSIONS:
            raise TerminalUnavailableError(
                "Too many open thread terminals. Close one and try again."
            )
        session = TerminalSession(key, launch)
        session.start(cols, rows, on_closed=self._forget)
        self._sessions[key] = session
        return session

    def _evict_one_idle(self) -> None:
        for session in list(self._sessions.values()):
            if not session.viewer_count:
                session.close()
                return

    def _forget(self, session: TerminalSession) -> None:
        if self._sessions.get(session.key) is session:
            del self._sessions[session.key]

    def close_all(self) -> None:
        for session in list(self._sessions.values()):
            session.close()


_registry: TerminalRegistry | None = None


def get_terminal_registry() -> TerminalRegistry:
    global _registry
    if _registry is None:
        _registry = TerminalRegistry()
    return _registry
