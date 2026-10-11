"""Coding-backend CLI logins: detect a missing or expired one, and explain it.

Openbase drives the user's own Claude Code or Codex CLI on their computer. When
that CLI is not signed in, the backend does not raise a clean error:

* Claude Code answers the turn with ``Not logged in · Please run /login`` (a
  wiped login) or ``Failed to authenticate ...`` / ``OAuth session expired``
  (an expired one), then marks the result ``is_error``.
* Codex retries for ~15 seconds and fails the turn with ``unexpected status
  401 Unauthorized: Missing bearer or basic authentication in header`` (no
  login) or a token-refresh error (an expired one).

Shown verbatim, both read like an Openbase app sign-in problem. This module is
the single source of truth for recognizing them (``backend_auth_failure``) and
for the one plain message every surface shows or speaks instead: thread chat,
socket errors, voice calls (GPT-Live and Classic) and setup.

Openbase Cloud backends ride the Openbase Cloud login, not a CLI login, so
their auth failures are never reported as a CLI sign-in problem here.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import socket
import subprocess
import sys

from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)
from openbase_coder_cli.paths import OPENBASE_BASE_DIR

CLI_LOGIN_BACKENDS = (CLAUDE_CODE_BACKEND, CODEX_BACKEND)
CLOUD_BACKENDS = (OPENBASE_CLOUD_BACKEND, OPENBASE_CLOUD_CODEX_BACKEND)

# Claude Code turn answers that mean "this CLI is not signed in". The first
# two are shared with the Openbase Cloud proxy path (Claude Code behind the
# proxy prints the same prefixes), so they only count for Claude Code when the
# backend is known or the text names /login.
CLAUDE_LOGIN_FAILURE_PREFIXES = (
    "Not logged in",
    "Failed to authenticate",
    "Invalid API key",
    "OAuth session expired",
    "OAuth token has expired",
)
# Codex turn errors that mean "this CLI is not signed in" (compared
# case-insensitively). A 401 from api.openai.com is the no-login answer; the
# rest are what Codex says when a stored ChatGPT login can no longer refresh.
CODEX_LOGIN_FAILURE_PREFIXES = (
    "unexpected status 401",
    "your access token could not be refreshed",
    "provided authentication token is expired",
    "your refresh token",
    "not logged in",
)
# A login failure is the whole message, never a passage inside a longer
# answer: an agent reply that merely mentions "401 Unauthorized" must stay
# untouched.
MAX_LOGIN_FAILURE_TEXT_CHARS = 1000

BACKEND_LABELS = {CLAUDE_CODE_BACKEND: "Claude Code", CODEX_BACKEND: "Codex"}


def _matches_claude(text: str) -> bool:
    return text.strip().startswith(CLAUDE_LOGIN_FAILURE_PREFIXES)


def _matches_codex(text: str) -> bool:
    return text.strip().lower().startswith(CODEX_LOGIN_FAILURE_PREFIXES)


def _configured_backends() -> list[str]:
    try:
        from openbase_coder_cli.services.selection import configured_coding_backends

        return configured_coding_backends()
    except Exception:
        return []


def backend_auth_failure(text: str | None, backend: str | None = None) -> str | None:
    """Which CLI backend (``claude_code`` / ``codex``) failed to sign in, or None.

    ``backend`` is the backend that produced ``text`` when the caller knows it.
    Without it, unambiguous wording decides (``/login`` is Claude Code, an
    api.openai.com 401 is Codex), then the configured coding backend.
    """
    if not text or not text.strip() or len(text) > MAX_LOGIN_FAILURE_TEXT_CHARS:
        return None
    if backend in CLOUD_BACKENDS:
        return None
    from openbase_coder_cli.cloud_model_errors import model_proxy_denial_message

    if model_proxy_denial_message(text) is not None:
        return None
    if backend == CLAUDE_CODE_BACKEND:
        return CLAUDE_CODE_BACKEND if _matches_claude(text) else None
    if backend == CODEX_BACKEND:
        return CODEX_BACKEND if _matches_codex(text) else None
    configured = _configured_backends()
    if configured and not any(b in CLI_LOGIN_BACKENDS for b in configured):
        # A cloud-only install has no CLI login to fix; its proxy failures
        # keep their own handling.
        return None
    lowered = text.lower()
    if _matches_claude(text) and "/login" in lowered:
        return CLAUDE_CODE_BACKEND
    if _matches_codex(text) and "api.openai.com" in lowered:
        return CODEX_BACKEND
    for candidate in configured:
        if candidate == CLAUDE_CODE_BACKEND and _matches_claude(text):
            return CLAUDE_CODE_BACKEND
        if candidate == CODEX_BACKEND and _matches_codex(text):
            return CODEX_BACKEND
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


# Set in every Maritime workspace's service environment.
MARITIME_ENV_MARKERS = ("MARITIME_AGENT_ID", "MARITIME_BACKEND_URL")


@functools.lru_cache(maxsize=1)
def on_cloud_workspace() -> bool:
    """Whether this backend is an Openbase Cloud workspace (Maritime).

    There the user has no terminal: a linked Codex or Claude Code account is
    relinked from the phone (Settings → AI Account), not with a CLI command.
    A Maritime workspace carries no DevSpace marker files; its runtime
    environment names it instead.
    """
    if any(os.environ.get(key) for key in MARITIME_ENV_MARKERS):
        return True
    from openbase_coder_cli.sync_pairing import is_cloud_workspace

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


def normalize_backend_error_text(text: str, backend: str | None = None) -> str:
    """Text safe to show a user in place of a raw backend error.

    A CLI login failure becomes :func:`backend_login_message`; an Openbase
    Cloud proxy denial becomes its plain message; anything else is unchanged.
    """
    from openbase_coder_cli.cloud_model_errors import normalize_model_proxy_error

    if failed_backend := backend_auth_failure(text, backend):
        return backend_login_message(failed_backend)
    return normalize_model_proxy_error(text)


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


def normalize_live_backend_error_text(text: str, backend: str | None = None) -> str:
    """Like :func:`normalize_backend_error_text`, for an error a turn just
    hit (not one re-rendered from history): a CLI login failure is also
    remembered, so the AI account card asks for a relink."""
    if failed_backend := backend_auth_failure(text, backend):
        mark_relink_needed(failed_backend)
        return backend_login_message(failed_backend)
    return normalize_backend_error_text(text, backend)


def backend_login_missing(backend: str | None) -> bool:
    """Whether ``backend``'s CLI is definitely not signed in on this computer.

    Fails open: an unknown backend, a missing binary or a probe error is not
    reported as a missing login.
    """
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
