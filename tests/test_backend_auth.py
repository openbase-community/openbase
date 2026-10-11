"""Coding-backend CLI login failures: classification, message, every surface."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openbase_coder_cli import backend_auth
from openbase_coder_cli.backend_auth import (
    backend_auth_failure,
    backend_login_message,
    backend_login_spoken_message,
    normalize_backend_error_text,
)
from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    OPENBASE_CLOUD_BACKEND,
)

# Captured 2026-10-10: `claude -p` with an empty config dir (Claude Code
# 2.1.296), and a Codex app-server turn with an empty CODEX_HOME (0.162.1).
CLAUDE_NOT_LOGGED_IN = "Not logged in · Please run /login"
CLAUDE_FAILED_TURN_ERROR = (
    "Not logged in · Please run /login (Claude Code returned an error result; "
    "task completion is unverified.)"
)
CODEX_NOT_LOGGED_IN = (
    "unexpected status 401 Unauthorized: Missing bearer or basic authentication "
    "in header, url: https://api.openai.com/v1/responses, cf-ray: "
    "a48a69b4da6698fc-SJC, request id: req_5b77c009b0b7418eb9d0316b2c42e20a"
)
CODEX_REFRESH_FAILED = (
    "Your access token could not be refreshed because your refresh token was "
    "already used. Please log out and sign in again."
)


@pytest.fixture(autouse=True)
def _computer(monkeypatch):
    monkeypatch.setattr(backend_auth, "computer_name", lambda: "Gabe's Mac")
    monkeypatch.setattr(
        backend_auth, "_configured_backends", lambda: [CLAUDE_CODE_BACKEND]
    )


@pytest.mark.parametrize(
    ("text", "backend", "expected"),
    [
        (CLAUDE_NOT_LOGGED_IN, None, CLAUDE_CODE_BACKEND),
        (CLAUDE_FAILED_TURN_ERROR, None, CLAUDE_CODE_BACKEND),
        (CLAUDE_NOT_LOGGED_IN, CLAUDE_CODE_BACKEND, CLAUDE_CODE_BACKEND),
        (
            "Failed to authenticate: OAuth session expired",
            CLAUDE_CODE_BACKEND,
            CLAUDE_CODE_BACKEND,
        ),
        (CODEX_NOT_LOGGED_IN, None, CODEX_BACKEND),
        (CODEX_NOT_LOGGED_IN, CODEX_BACKEND, CODEX_BACKEND),
        (CODEX_REFRESH_FAILED, CODEX_BACKEND, CODEX_BACKEND),
        # A cloud backend has no CLI login to fix.
        (CLAUDE_NOT_LOGGED_IN, OPENBASE_CLOUD_BACKEND, None),
        # Real answers that merely mention an auth error stay untouched.
        ("The API returned 401 Unauthorized, so I added a token refresh.", None, None),
        ("Here is how /login works in the app.", None, None),
        ("", None, None),
        (None, None, None),
    ],
)
def test_backend_auth_failure_classifies(text, backend, expected) -> None:
    assert backend_auth_failure(text, backend) == expected


def test_proxy_denial_is_not_a_cli_login_failure() -> None:
    denial = (
        'Failed to authenticate. API Error: 403 {"detail":"Monthly Openbase model '
        'proxy spend limit reached."}'
    )
    assert backend_auth_failure(denial, CLAUDE_CODE_BACKEND) is None


def test_cloud_only_install_never_reports_cli_login(monkeypatch) -> None:
    monkeypatch.setattr(
        backend_auth, "_configured_backends", lambda: [OPENBASE_CLOUD_BACKEND]
    )
    assert backend_auth_failure(CLAUDE_NOT_LOGGED_IN) is None


def test_messages_name_the_computer_and_the_fix() -> None:
    claude = backend_login_message(CLAUDE_CODE_BACKEND)
    assert claude == (
        "Claude Code isn't signed in on your computer (Gabe's Mac). On that "
        "computer, open a terminal and run `claude`, then type /login (or run "
        "`claude login`). Then try again."
    )
    assert backend_login_message(CODEX_BACKEND) == (
        "Codex isn't signed in on your computer (Gabe's Mac). On that computer, "
        "open a terminal and run `codex login`. Then try again."
    )
    spoken = backend_login_spoken_message(CLAUDE_CODE_BACKEND)
    assert spoken == (
        "Claude Code isn't signed in on your computer, Gabe's Mac. On that "
        "computer, open a terminal, run claude, and type /login. Then try again."
    )
    assert "`" not in spoken and "(" not in spoken
    assert "`" not in backend_login_spoken_message(CODEX_BACKEND)


def test_normalize_replaces_only_login_failures() -> None:
    assert normalize_backend_error_text(CLAUDE_NOT_LOGGED_IN) == backend_login_message(
        CLAUDE_CODE_BACKEND
    )
    assert normalize_backend_error_text(CODEX_NOT_LOGGED_IN) == backend_login_message(
        CODEX_BACKEND
    )
    assert normalize_backend_error_text("All done.") == "All done."


def test_thread_chat_shows_login_message_instead_of_raw_sentinel() -> None:
    from openbase_coder_cli.thread_sync.thread_messages import turn_messages

    messages = turn_messages(
        {
            "items": [
                {"type": "userMessage", "content": [{"type": "text", "text": "hi"}]},
                {"type": "agentMessage", "text": CLAUDE_NOT_LOGGED_IN},
            ]
        }
    )
    assert messages[-1]["text"] == backend_login_message(CLAUDE_CODE_BACKEND)


def test_failed_turn_socket_error_is_the_login_message() -> None:
    from openbase_coder_cli.thread_sync.session_manager import _turn_failure_message

    assert _turn_failure_message(
        {"error": {"message": CODEX_NOT_LOGGED_IN}}
    ) == backend_login_message(CODEX_BACKEND)
    assert _turn_failure_message(
        {"error": {"message": CLAUDE_FAILED_TURN_ERROR}}
    ) == backend_login_message(CLAUDE_CODE_BACKEND)


@pytest.mark.parametrize(
    ("own", "expected_backend"),
    [
        ({"lastUsefulMessage": CLAUDE_NOT_LOGGED_IN}, CLAUDE_CODE_BACKEND),
        ({"lastError": CODEX_NOT_LOGGED_IN}, CODEX_BACKEND),
    ],
)
def test_failed_turn_speech_keeps_its_own_login_error(own, expected_backend) -> None:
    from openbase_coder_cli.livekit_agent.super_agents_speech import (
        _speech_text_from_progress,
    )

    turn = {"turnId": "t1", "status": "failed", **own}
    text = _speech_text_from_progress(
        {"status": "failed", "turnId": "t1", "turn": turn}, turn_id="t1"
    )
    assert backend_auth_failure(text) == expected_backend


@pytest.mark.parametrize(
    ("backend", "raw"),
    [
        (CLAUDE_CODE_BACKEND, CLAUDE_FAILED_TURN_ERROR),
        (CODEX_BACKEND, CODEX_NOT_LOGGED_IN),
    ],
)
def test_voice_flags_and_speaks_login_failure(backend, raw) -> None:
    from openbase_coder_cli.livekit_agent import super_agents_client as client

    auth_failed = client._flag_backend_auth_failure(raw, backend=backend)
    assert auth_failed is True
    spoken = client._safe_spoken_answer(raw, auth_failed=True, backend=backend)
    assert spoken == backend_login_spoken_message(backend)


@pytest.mark.asyncio
async def test_failed_voice_turn_without_text_uses_login_check(
    tmp_path, monkeypatch
) -> None:
    """The reported bug: a Claude Code dispatcher turn fails with no speech;
    the CLI says it is logged out, so the call says how to sign in."""
    from openbase_coder_cli.livekit_agent import super_agents_client as client
    from tests.test_livekit_agent_super_agents_client import FakeSuperAgentsBackend

    monkeypatch.setattr(client, "backend_login_missing", lambda backend: True)
    monkeypatch.setattr(
        client,
        "verified_claude_auth_status",
        lambda: SimpleNamespace(logged_in=False, raw_output="{}"),
    )

    class LoggedOutBackend(FakeSuperAgentsBackend):
        backend = CLAUDE_CODE_BACKEND

        async def progress_by_label(self, input_data):
            turn = {"turnId": input_data.turn_id, "status": "failed"}
            return {"status": "failed", "turnId": input_data.turn_id, "turn": turn}

    voice = client.SuperAgentsLiveKitClient(
        cwd=str(tmp_path),
        state_path=str(tmp_path / "voice.json"),
        backend_client=LoggedOutBackend(),
    )
    result = await voice.run_turn("What is seven times nine?")
    assert result["status"] == "failed"
    assert result["_livekit_backend_auth_failure"] is True
    assert result["_livekit_speech_text"] == backend_login_spoken_message(
        CLAUDE_CODE_BACKEND
    )


def test_live_commentary_suppresses_raw_login_errors() -> None:
    from openbase_coder_cli.livekit_agent.super_agents_client import (
        _looks_like_raw_backend_error,
    )

    assert _looks_like_raw_backend_error(CODEX_NOT_LOGGED_IN)
    assert _looks_like_raw_backend_error(CLAUDE_NOT_LOGGED_IN)
    assert not _looks_like_raw_backend_error(
        backend_login_spoken_message(CLAUDE_CODE_BACKEND)
    )


def test_cloud_workspace_message_points_at_the_ai_account_setting() -> None:
    assert backend_login_message(CODEX_BACKEND, cloud=True) == (
        "Codex isn't signed in on your cloud workspace. Relink it in "
        "Settings → AI Account, or switch back to Openbase Cloud."
    )
    spoken = backend_login_spoken_message(CLAUDE_CODE_BACKEND, cloud=True)
    assert spoken.startswith("Claude Code isn't signed in on your cloud workspace.")
    assert "→" not in spoken


def test_cloud_wording_follows_the_workspace(monkeypatch) -> None:
    from openbase_coder_cli import backend_auth

    monkeypatch.setattr(backend_auth, "on_cloud_workspace", lambda: True)
    assert "cloud workspace" in backend_login_message(CLAUDE_CODE_BACKEND)


def test_live_login_failure_marks_relink_until_cleared() -> None:
    from openbase_coder_cli import backend_auth
    from openbase_coder_cli.thread_sync.session_manager import _turn_failure_message

    assert backend_auth.relink_needed_backends() == set()
    _turn_failure_message({"error": {"message": CODEX_NOT_LOGGED_IN}})
    assert backend_auth.relink_needed_backends() == {CODEX_BACKEND}
    backend_auth.clear_relink_needed(CODEX_BACKEND)
    assert backend_auth.relink_needed_backends() == set()


def test_rendering_history_does_not_mark_relink() -> None:
    from openbase_coder_cli import backend_auth

    backend_auth.normalize_backend_error_text(CODEX_NOT_LOGGED_IN)
    assert backend_auth.relink_needed_backends() == set()


def test_maritime_environment_counts_as_a_cloud_workspace(monkeypatch) -> None:
    from openbase_coder_cli import backend_auth

    # conftest replaces the detector; test the real one.
    monkeypatch.undo()
    backend_auth.on_cloud_workspace.cache_clear()
    monkeypatch.setenv("MARITIME_AGENT_ID", "agent-1")
    try:
        assert backend_auth.on_cloud_workspace() is True
    finally:
        backend_auth.on_cloud_workspace.cache_clear()
