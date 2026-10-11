"""Persistent pseudo-terminal sessions an agent can read and type into.

Interactive logins (``gcloud auth login``, ``az login``, ``heroku login -i``,
``codex login`` ...) print URLs, codes and prompts, and wait for input.
An agent that inspects that output can decide what each login needs: open a
page on the user's phone, forward a localhost callback, relay a code, or type
an answer. Each session is a small detached holder process that owns the
pty, so it outlives the agent's turn:

* output goes to ``<state>/<name>/output.log`` (owner-only), ANSI-stripped
  on read, with every secret the agent typed replaced by ``[secret]``;
* input arrives through ``<state>/<name>/input`` (a FIFO) as JSON lines;
* the session ends when the command exits, after ``IDLE_TIMEOUT_SECONDS``
  without output or input, or after ``MAX_LIFETIME_SECONDS``.

Secrets are kept only in the holder's memory, never written to disk.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from openbase_coder_cli.paths import OPENBASE_BASE_DIR

STATE_DIR = OPENBASE_BASE_DIR / "pty"
IDLE_TIMEOUT_SECONDS = 30 * 60
MAX_LIFETIME_SECONDS = 2 * 60 * 60
MAX_LOG_BYTES = 2 * 1024 * 1024
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
SECRET_MARK = "[secret]"

_ANSI = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>]"
)


class PtySessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionPaths:
    root: Path

    @property
    def output(self) -> Path:
        return self.root / "output.log"

    @property
    def input(self) -> Path:
        return self.root / "input"

    @property
    def meta(self) -> Path:
        return self.root / "meta.json"

    @property
    def cursor(self) -> Path:
        return self.root / "cursor"


def session_paths(name: str) -> SessionPaths:
    if NAME_RE.fullmatch(name) is None:
        raise PtySessionError(
            "Session names are 1-40 lowercase letters, digits or dashes."
        )
    return SessionPaths(STATE_DIR / name)


def clean_output(raw: str) -> str:
    """Terminal output as plain text: no escapes, carriage returns folded."""
    text = _ANSI.sub("", raw).replace("\r\n", "\n")
    lines = []
    for line in text.split("\n"):
        # A bare \r redraws the line; keep what was drawn last.
        lines.append(line.rsplit("\r", 1)[-1])
    return "\n".join(lines)


def _read_meta(paths: SessionPaths) -> dict:
    try:
        payload = json.loads(paths.meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_meta(paths: SessionPaths, payload: dict) -> None:
    tmp = paths.meta.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, paths.meta)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def session_status(name: str) -> dict:
    paths = session_paths(name)
    meta = _read_meta(paths)
    if not meta:
        raise PtySessionError(f"No pty session named {name}.")
    running = meta.get("exit_code") is None and _alive(meta.get("holder_pid"))
    return {
        "name": name,
        "command": meta.get("command"),
        "running": running,
        "exit_code": meta.get("exit_code"),
        "ended_reason": meta.get("ended_reason"),
        "started_at": meta.get("started_at"),
    }


def list_sessions() -> list[dict]:
    if not STATE_DIR.is_dir():
        return []
    sessions = []
    for child in sorted(STATE_DIR.iterdir()):
        if child.is_dir() and NAME_RE.fullmatch(child.name):
            with contextlib.suppress(PtySessionError):
                sessions.append(session_status(child.name))
    return sessions


def start(name: str, command: list[str], *, cwd: str | None = None) -> dict:
    """Start ``command`` in a new detached pty session called ``name``."""
    if not command:
        raise PtySessionError("Give the command to run after --.")
    paths = session_paths(name)
    if paths.meta.exists():
        status = session_status(name)
        if status["running"]:
            raise PtySessionError(
                f"Session {name} is still running; stop it or pick another name."
            )
        _remove(paths)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    paths.root.mkdir(mode=0o700)
    os.mkfifo(paths.input, 0o600)
    fd = os.open(paths.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    _write_meta(
        paths,
        {"command": command, "cwd": cwd, "started_at": time.time(), "exit_code": None},
    )
    holder = subprocess.Popen(
        [sys.executable, "-m", "openbase_coder_cli.pty_session", "hold", name],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=cwd or None,
        close_fds=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        meta = _read_meta(paths)
        if meta.get("child_pid") or meta.get("exit_code") is not None:
            break
        if holder.poll() is not None:
            break
        time.sleep(0.05)
    return session_status(name)


def read(name: str, *, wait: float = 0.0, everything: bool = False) -> dict:
    """Output since the last read (or all of it), plus the session status.

    ``wait`` blocks up to that many seconds for new output or an exit.
    """
    paths = session_paths(name)
    if not paths.meta.exists():
        raise PtySessionError(f"No pty session named {name}.")
    offset = 0
    if not everything:
        with contextlib.suppress(OSError, ValueError):
            offset = int(paths.cursor.read_text(encoding="utf-8").strip() or 0)
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        size = paths.output.stat().st_size if paths.output.exists() else 0
        status = session_status(name)
        if size > offset or not status["running"] or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    with paths.output.open("rb") as stream:
        stream.seek(offset)
        data = stream.read()
    new_offset = offset + len(data)
    cursor_tmp = paths.cursor.with_suffix(".tmp")
    cursor_tmp.write_text(str(new_offset), encoding="utf-8")
    os.replace(cursor_tmp, paths.cursor)
    return {
        **status,
        "output": clean_output(data.decode("utf-8", errors="replace")),
    }


def send(name: str, text: str, *, enter: bool = True, secret: bool = False) -> None:
    """Type ``text`` into the session; a secret is redacted from its output."""
    paths = session_paths(name)
    status = session_status(name)
    if not status["running"]:
        raise PtySessionError(f"Session {name} is not running.")
    if "\n" in text or "\r" in text:
        raise PtySessionError("Send one line at a time.")
    message = json.dumps({"text": text, "enter": enter, "secret": secret}) + "\n"
    fd = os.open(paths.input, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, message.encode("utf-8"))
    finally:
        os.close(fd)


def stop(name: str) -> None:
    paths = session_paths(name)
    meta = _read_meta(paths)
    for key in ("child_pid", "holder_pid"):
        pid = meta.get(key)
        if pid and _alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGTERM)


def _remove(paths: SessionPaths) -> None:
    for child in (paths.output, paths.input, paths.meta, paths.cursor):
        child.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        paths.root.rmdir()


def _redact(text: bytes, secrets: list[bytes]) -> bytes:
    for secret in secrets:
        text = text.replace(secret, SECRET_MARK.encode())
    return text


def _hold(name: str) -> int:
    """The holder process: run the command on a pty until it ends."""
    import pty

    paths = session_paths(name)
    meta = _read_meta(paths)
    command = meta["command"]
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("BROWSER", "openbase-browser")
    env.pop("DJANGO_SETTINGS_MODULE", None)
    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            os.execvpe(command[0], command, env)
        except OSError as exc:
            os.write(2, f"cannot run {command[0]}: {exc}\n".encode())
            os._exit(127)
    meta.update(holder_pid=os.getpid(), child_pid=pid)
    _write_meta(paths, meta)
    input_fd = os.open(paths.input, os.O_RDWR | os.O_NONBLOCK)
    secrets: list[bytes] = []
    pending_input = b""
    started = last_activity = time.monotonic()
    exit_code: int | None = None
    reason = "exited"
    tail = b""
    with paths.output.open("ab") as log:
        while True:
            now = time.monotonic()
            if now - last_activity > IDLE_TIMEOUT_SECONDS:
                reason = "idle timeout"
                break
            if now - started > MAX_LIFETIME_SECONDS:
                reason = "lifetime limit"
                break
            try:
                ready, _, _ = select.select([master_fd, input_fd], [], [], 1.0)
            except InterruptedError:
                continue
            if not ready and tail:
                # Quiet terminal: nothing more can complete a held secret.
                log.write(_redact(tail, secrets))
                log.flush()
                tail = b""
            if master_fd in ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                last_activity = time.monotonic()
                # Redact across chunk boundaries: hold back a secret's length.
                data = _redact(tail + chunk, secrets)
                hold = max((len(s) for s in secrets), default=0) - 1
                if hold > 0 and len(data) > hold:
                    tail, data = data[-hold:], data[:-hold]
                elif hold > 0:
                    tail, data = data, b""
                else:
                    tail = b""
                if log.tell() < MAX_LOG_BYTES:
                    log.write(data)
                    log.flush()
            if input_fd in ready:
                with contextlib.suppress(BlockingIOError):
                    pending_input += os.read(input_fd, 65536)
                while b"\n" in pending_input:
                    line, pending_input = pending_input.split(b"\n", 1)
                    with contextlib.suppress(ValueError, TypeError, KeyError):
                        request = json.loads(line)
                        text = str(request["text"]).encode("utf-8")
                        if request.get("secret") and text:
                            secrets.append(text)
                        os.write(
                            master_fd,
                            text + (b"\r" if request.get("enter", True) else b""),
                        )
                        last_activity = time.monotonic()
        if tail:
            log.write(_redact(tail, secrets))
    if reason != "exited":
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        _, wait_status = os.waitpid(pid, 0)
        exit_code = os.waitstatus_to_exitcode(wait_status)
    secrets.clear()
    meta = _read_meta(paths)
    meta.update(
        exit_code=exit_code if exit_code is not None else -1, ended_reason=reason
    )
    _write_meta(paths, meta)
    with contextlib.suppress(OSError):
        os.close(master_fd)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "hold":
        raise SystemExit(_hold(sys.argv[2]))
    raise SystemExit("usage: python -m openbase_coder_cli.pty_session hold NAME")
