"""Coding-backend CLI logins: detect a missing or expired one, and explain it.

Openbase drives the user's own Claude Code or Codex CLI on their computer. When
that CLI is not signed in, its raw error ("Not logged in · Please run /login",
or Codex's "unexpected status 401 Unauthorized ...") reads like an Openbase
app sign-in problem. This module is the single source of truth for
recognizing that case and for the one plain message every surface shows or
speaks instead: thread chat, socket errors, voice calls (GPT-Live and
Classic), the Dispatcher and setup.

Recognition uses structured signals only, never error text:

* the turn's ``errorKind`` (Super Agents records the Claude Agent SDK's
  ``AssistantMessage.error`` and maps Codex's ``codexErrorInfo`` HTTP 401) is
  ``authentication_failed``;
* for a turn failing live, the CLI's own login state (``claude auth status``
  JSON, Codex ``auth.json``) says it is signed out.

Openbase Cloud backends ride the Openbase Cloud login, not a CLI login, so
their failures are never reported as a CLI sign-in problem here.
"""

from __future__ import annotations

import contextlib
import functools
import json
import socket
import subprocess
import sys
import time
from typing import Any

from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)
from openbase_coder_cli.paths import OPENBASE_BASE_DIR

CLI_LOGIN_BACKENDS = (CLAUDE_CODE_BACKEND, CODEX_BACKEND)
CLOUD_BACKENDS = (OPENBASE_CLOUD_BACKEND, OPENBASE_CLOUD_CODEX_BACKEND)

AUTHENTICATION_FAILED = "authentication_failed"
FAILED_TURN_STATUSES = ("failed", "error", "systemerror")
# A logged-out answer is re-checked after this long; a live failure path asks
# at most this often per backend.
LOGIN_STATE_TTL_SECONDS = 5.0

BACKEND_LABELS = {CLAUDE_CODE_BACKEND: "Claude Code", CODEX_BACKEND: "Codex"}


def _configured_backends() -> list[str]:
    try:
        from openbase_coder_cli.services.selection import configured_coding_backends

        return configured_coding_backends()
    except Exception:
        return []


def cli_login_backend(backend: str | None = None) -> str | None:
    """The CLI-login backend a turn ran on (``claude_code`` / ``codex``).

    ``backend`` is the turn's configured backend when the caller knows it;
    otherwise the install's primary configured backend. None for Openbase
    Cloud backends, which have no CLI login.
    """
    if backend is None:
        configured = _configured_backends()
        backend = configured[0] if configured else None
    return backend if backend in CLI_LOGIN_BACKENDS else None


def turn_failed(turn: dict[str, Any] | None) -> bool:
    """Whether a turn payload (or turn notification params) records a failure."""
    if not isinstance(turn, dict):
        return False
    status = turn.get("status")
    if isinstance(status, dict):
        status = status.get("type")
    if isinstance(status, str) and status.lower() in FAILED_TURN_STATUSES:
        return True
    return turn.get("error") not in (None, "", {})


def turn_reports_authentication_failure(turn: dict[str, Any] | None) -> bool:
    """Whether a turn's structured error kind says its backend couldn't sign in."""
    from super_agents.app_protocol import turn_error_kind

    return turn_error_kind(turn) == AUTHENTICATION_FAILED


def failed_turn_login_backend(
    turn: dict[str, Any] | None,
    backend: str | None = None,
    *,
    check_login: bool = False,
) -> str | None:
    """The CLI backend whose missing login made ``turn`` fail, or None.

    A failed turn qualifies when its structured error kind is
    ``authentication_failed``, or, with ``check_login`` (a turn failing now,
    not one re-rendered from history), when the CLI itself reports it is
    signed out.
    """
    cli_backend = cli_login_backend(backend)
    if cli_backend is None or not turn_failed(turn):
        return None
    if turn_reports_authentication_failure(turn):
        return cli_backend
    if check_login and backend_login_missing(cli_backend):
        return cli_backend
    return None


@functools.lru_cache(maxsize=1)
def computer_name() -> str:
    """This computer's user-facing name ("Gabe's MacBook Pro")."""
    if sys.platform == "darwin":
        try:
            completed = subprocess.run(
                ["scutil", "--get", "ComputerName"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            name = completed.stdout.strip()
            if name:
                return name
    return socket.gethostname().split(".")[0] or "this computer"


def _where(name: str | None) -> str:
    return f"on your computer ({name})" if name else "on your computer"


@functools.lru_cache(maxsize=1)
def on_cloud_workspace() -> bool:
    """Whether this backend is an Openbase Cloud workspace (Maritime).

    There the user has no terminal: a linked Codex or Claude Code account is
    relinked from the phone (Settings → AI Account), not with a CLI command.
    """
    from openbase_coder_cli.services.cloud_workspace import is_cloud_workspace

    return is_cloud_workspace()


def _uses_cloud_wording(cloud: bool | None) -> bool:
    return on_cloud_workspace() if cloud is None else cloud


def backend_login_message(
    backend: str, *, name: str | None = None, cloud: bool | None = None
) -> str:
    """The one written message for a CLI backend that is not signed in.

    ``name`` defaults to this computer's name; pass ``""`` to omit it.
    ``cloud`` defaults to whether this is a cloud workspace.
    """
    if _uses_cloud_wording(cloud):
        label = BACKEND_LABELS.get(backend, "Your AI account")
        return (
            f"{label} isn't signed in on your cloud workspace. Relink it in "
            "Settings → AI Account, or switch back to Openbase Cloud."
        )
    where = _where(computer_name() if name is None else name)
    if backend == CODEX_BACKEND:
        return (
            f"Codex isn't signed in {where}. On that computer, open a terminal "
            "and run `codex login`. Then try again."
        )
    return (
        f"Claude Code isn't signed in {where}. On that computer, open a "
        "terminal and run `claude`, then type /login (or run `claude login`). "
        "Then try again."
    )


def backend_login_spoken_message(
    backend: str, *, name: str | None = None, cloud: bool | None = None
) -> str:
    """The same message as a voice call says it: no markdown or punctuation
    that reads badly aloud."""
    if _uses_cloud_wording(cloud):
        label = BACKEND_LABELS.get(backend, "Your AI account")
        return (
            f"{label} isn't signed in on your cloud workspace. Relink it in "
            "the app's Settings, under AI Account, or switch back to "
            "Openbase Cloud."
        )
    resolved = computer_name() if name is None else name
    where = f"on your computer, {resolved}" if resolved else "on your computer"
    if backend == CODEX_BACKEND:
        return (
            f"Codex isn't signed in {where}. On that computer, open a terminal "
            "and run codex login. Then try again."
        )
    return (
        f"Claude Code isn't signed in {where}. On that computer, open a "
        "terminal, run claude, and type /login. Then try again."
    )


def login_failure_message(
    turn: dict[str, Any] | None,
    backend: str | None = None,
    *,
    live: bool = False,
) -> str | None:
    """The plain sign-in message for a turn that failed on a signed-out CLI.

    ``live`` marks a turn failing now (not one re-rendered from history): its
    CLI login is checked directly, and the failure is remembered so the AI
    account card asks for a relink.
    """
    failed_backend = failed_turn_login_backend(turn, backend, check_login=live)
    if failed_backend is None:
        return None
    if live:
        mark_relink_needed(failed_backend)
    return backend_login_message(failed_backend)


RELINK_STATE_PATH = OPENBASE_BASE_DIR / "ai-account-relink.json"


def relink_needed_backends() -> set[str]:
    """CLI backends whose login failed during a live turn since they were
    last linked (the AI account card shows them as needing a relink)."""
    try:
        data = json.loads(RELINK_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {b for b in data if b in CLI_LOGIN_BACKENDS}


def _write_relink_needed(backends: set[str]) -> None:
    with contextlib.suppress(OSError):
        RELINK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = RELINK_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(backends)), encoding="utf-8")
        tmp.replace(RELINK_STATE_PATH)


def mark_relink_needed(backend: str) -> None:
    current = relink_needed_backends()
    if backend in CLI_LOGIN_BACKENDS and backend not in current:
        _write_relink_needed(current | {backend})


def clear_relink_needed(backend: str) -> None:
    current = relink_needed_backends()
    if backend in current:
        _write_relink_needed(current - {backend})


_login_state_cache: dict[str, tuple[float, bool]] = {}


def backend_login_missing(backend: str | None, *, fresh: bool = False) -> bool:
    """Whether ``backend``'s CLI is definitely not signed in on this computer.

    Fails open: an unknown backend, a missing binary or a probe error is not
    reported as a missing login. Answers are reused for
    ``LOGIN_STATE_TTL_SECONDS`` unless ``fresh``.
    """
    if backend not in CLI_LOGIN_BACKENDS:
        return False
    cached = _login_state_cache.get(backend)
    now = time.monotonic()
    if not fresh and cached and now - cached[0] < LOGIN_STATE_TTL_SECONDS:
        return cached[1]
    missing = _probe_login_missing(backend)
    _login_state_cache[backend] = (now, missing)
    return missing


def _probe_login_missing(backend: str) -> bool:
    if backend == CLAUDE_CODE_BACKEND:
        from openbase_coder_cli.claude_auth import claude_auth_status

        try:
            status = claude_auth_status(timeout=10)
        except Exception:
            return False
        # ``claude auth status`` exits 1 when logged out; 127/124 mean the
        # probe itself could not answer.
        return status.returncode in (0, 1) and not status.logged_in
    if backend == CODEX_BACKEND:
        from openbase_coder_cli.services.onboarding import codex_auth_present

        try:
            return not codex_auth_present()
        except Exception:
            return False
    return False


def run_backend_login(backend: str) -> int:
    """Run ``backend``'s own interactive CLI login in this terminal."""
    import shutil

    from openbase_coder_cli.backend_binaries import find_backend_binary

    if backend == CODEX_BACKEND:
        command = str(find_backend_binary("codex") or shutil.which("codex") or "codex")
        return subprocess.call([command, "login"])
    from openbase_coder_cli.claude_auth import run_claude_login

    command = find_backend_binary("claude")
    return run_claude_login(claude_command=str(command) if command else None)
