"""Claude Code login status for the shared ~/.claude home.

Openbase uses the user's own Claude Code login directly (one credential
store, no copies), so this module only reports whether that login works —
there is no bridging or healing to do.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openbase_coder_cli.paths import CLAUDE_CONFIG_DIR

CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
# A coding backend prints turn-level auth failures as ordinary result text
# (exit code 0) instead of erroring. An expired-but-present login answers
# "Failed to authenticate. API Error: 401 Invalid bearer token" or "Failed to
# authenticate: OAuth session expired and could not be refreshed"; a wiped
# credential answers "Not logged in · Please run /login". Both Claude Code and
# Codex (including the openbase_cloud Codex provider when its key is absent)
# emit these same sentinels, so the classifier is deliberately backend-agnostic.
BACKEND_AUTH_FAILURE_PREFIXES = ("Failed to authenticate", "Not logged in")
# Backwards-compatible alias (kept for existing importers).
CLAUDE_AUTH_FAILURE_PREFIXES = BACKEND_AUTH_FAILURE_PREFIXES
CLAUDE_AUTH_PROBE_PROMPT = "Reply with the single word ok."
CLAUDE_AUTH_PROBE_TIMEOUT_SECONDS = 90


@dataclass(frozen=True)
class ClaudeAuthStatus:
    logged_in: bool
    raw_output: str
    returncode: int


def claude_auth_status(*, claude_command: str | None = None) -> ClaudeAuthStatus:
    command = claude_command or shutil.which("claude") or "claude"
    try:
        completed = subprocess.run(
            [command, "auth", "status"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ClaudeAuthStatus(
            logged_in=False,
            raw_output="Claude Code CLI not found on PATH.",
            returncode=127,
        )

    output = (completed.stdout or completed.stderr).strip()
    logged_in = False
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        logged_in = "loggedIn" in output and "true" in output
    else:
        logged_in = bool(payload.get("loggedIn"))
    return ClaudeAuthStatus(
        logged_in=logged_in,
        raw_output=output,
        returncode=completed.returncode,
    )


def is_backend_auth_failure_text(text: str | None) -> bool:
    """Whether a turn's answer is a coding backend's spoken-back auth failure.

    Matches the Claude Code *and* Codex sentinels (see
    ``BACKEND_AUTH_FAILURE_PREFIXES``); a turn that "answers" with one of these
    is a login failure masquerading as a normal reply, not a real answer.
    """
    return bool(text) and text.strip().startswith(BACKEND_AUTH_FAILURE_PREFIXES)


def is_claude_auth_failure_text(text: str | None) -> bool:
    """Backwards-compatible alias for :func:`is_backend_auth_failure_text`."""
    return is_backend_auth_failure_text(text)


def read_claude_credential_expiry() -> float | None:
    """Epoch-ms expiry of the stored Claude OAuth access token."""
    payload: dict[str, Any] = {}
    if platform.system() == "Darwin":
        secret = _read_keychain_secret(CLAUDE_KEYCHAIN_SERVICE)
        if secret:
            try:
                parsed = json.loads(secret)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                payload = parsed
    else:
        payload = _read_json_object(CLAUDE_CONFIG_DIR / ".credentials.json")
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    expires = oauth.get("expiresAt")
    return float(expires) if isinstance(expires, int | float) else None


def _read_keychain_secret(service: str) -> str | None:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    secret = result.stdout.strip()
    return secret or None


def probe_claude_auth(*, claude_command: str | None = None) -> ClaudeAuthStatus:
    """Run a minimal real turn to see whether the login actually works.

    ``claude auth status`` reports cached account state and keeps saying
    ``loggedIn: true`` after the OAuth tokens die, so only a real API call can
    tell. A successful probe also makes the CLI refresh and persist fresh
    tokens as a side effect. Inconclusive outcomes (timeout) count as logged
    in so transient stalls never report a false logout.
    """
    command = claude_command or shutil.which("claude") or "claude"
    try:
        completed = subprocess.run(
            [command, "-p", CLAUDE_AUTH_PROBE_PROMPT, "--model", "haiku"],
            check=False,
            capture_output=True,
            text=True,
            timeout=CLAUDE_AUTH_PROBE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return ClaudeAuthStatus(
            logged_in=False,
            raw_output="Claude Code CLI not found on PATH.",
            returncode=127,
        )
    except subprocess.TimeoutExpired:
        return ClaudeAuthStatus(
            logged_in=True,
            raw_output="Claude auth probe timed out; assuming the login is intact.",
            returncode=124,
        )
    output = (completed.stdout or completed.stderr).strip()
    return ClaudeAuthStatus(
        logged_in=not is_claude_auth_failure_text(output),
        raw_output=output,
        returncode=completed.returncode,
    )


def verified_claude_auth_status(
    *, claude_command: str | None = None
) -> ClaudeAuthStatus:
    """Auth status that catches expired-but-cached logins.

    When ``claude auth status`` claims a login but the stored access token is
    past its expiry, verify with a probe turn: the probe either refreshes the
    tokens (still logged in) or surfaces the real auth failure.
    """
    status = claude_auth_status(claude_command=claude_command)
    if not status.logged_in:
        return status
    expiry_ms = read_claude_credential_expiry()
    if expiry_ms is None or expiry_ms > time.time() * 1000:
        return status
    probe = probe_claude_auth(claude_command=claude_command)
    if probe.logged_in:
        return status
    return probe


def run_claude_login(
    *,
    claude_command: str | None = None,
    sso: bool = False,
    email: str | None = None,
) -> int:
    command = claude_command or shutil.which("claude") or "claude"
    args = [command, "auth", "login", "--claudeai"]
    if sso:
        args.append("--sso")
    if email:
        args.extend(["--email", email])
    return subprocess.call(args)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
