"""Coding-backend CLI login failures: structured classification, one message."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openbase_coder_cli import backend_auth
from openbase_coder_cli.backend_auth import (
    backend_login_message,
    backend_login_spoken_message,
    failed_turn_login_backend,
    login_failure_message,
)
from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)

# Captured 2026-10-10/11 with each CLI signed out: Claude Code 2.1.296 via the
# Agent SDK 0.2.93 (AssistantMessage.error="authentication_failed"), and a
# Codex 0.162.1 app-server turn (codexErrorInfo httpStatusCode 401).
CLAUDE_TURN = {
    "turnId": "t1",
    "status": "failed",
    "errorKind": "authentication_failed",
    "lastError": (
        "Not logged in · Please run /login (Claude Code returned an error "
        "result; task completion is unverified.)"
    ),
    "items": [
        {"type": "userMessage", "content": [{"type": "text", "text": "hi"}]},
        {"type": "agentMessage", "text": "Not logged in · Please run /login"},
    ],
}
CODEX_ERROR = {
    "message": (
        "unexpected status 401 Unauthorized: Missing bearer or basic "
        "authentication in header, url: https://api.openai.com/v1/responses"
    ),
    "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}},
}
CODEX_TURN = {
    "id": "t2",
    "status": "failed",
    "error": CODEX_ERROR,
    "items": [{"type": "userMessage", "content": [{"type": "text", "text": "hi"}]}],
}


@pytest.fixture(autouse=True)
def _computer(monkeypatch):
    monkeypatch.setattr(backend_auth, "computer_name", lambda: "Gabe's Mac")
    monkeypatch.setattr(
        backend_auth, "_configured_backends", lambda: [CLAUDE_CODE_BACKEND]
    )
    monkeypatch.setattr(backend_auth, "_probe_login_missing", lambda _backend: False)
    backend_auth._login_state_cache.clear()


@pytest.mark.parametrize(
    ("turn", "backend", "expected"),
    [
        (CLAUDE_TURN, CLAUDE_CODE_BACKEND, CLAUDE_CODE_BACKEND),
        (CLAUDE_TURN, None, CLAUDE_CODE_BACKEND),
        (CODEX_TURN, CODEX_BACKEND, CODEX_BACKEND),
        # Openbase Cloud backends have no CLI login to fix.
        (CLAUDE_TURN, OPENBASE_CLOUD_BACKEND, None),
        (CODEX_TURN, OPENBASE_CLOUD_CODEX_BACKEND, None),
        # Not failed, or failed for another structured reason.
        ({**CLAUDE_TURN, "status": "completed", "errorKind": None}, None, None),
        ({**CLAUDE_TURN, "errorKind": "rate_limit"}, None, None),
    ],
)
def test_failed_turn_login_backend_uses_structured_error_kind(
    turn, backend, expected
) -> None:
    assert failed_turn_login_backend(turn, backend) == expected


def test_error_text_alone_never_classifies() -> None:
    """The sign-in wording without a structured kind is not a login failure."""
    turn = {**CLAUDE_TURN, "errorKind": None}
    assert failed_turn_login_backend(turn, CLAUDE_CODE_BACKEND) is None
    codex = {**CODEX_TURN, "error": {"message": CODEX_ERROR["message"]}}
    assert failed_turn_login_backend(codex, CODEX_BACKEND) is None


def test_live_failure_checks_the_cli_login_state(monkeypatch) -> None:
    turn = {"turnId": "t3", "status": "failed", "lastError": "anything"}
    assert failed_turn_login_backend(turn, CLAUDE_CODE_BACKEND) is None
    assert (
        failed_turn_login_backend(turn, CLAUDE_CODE_BACKEND, check_login=True) is None
    )
    monkeypatch.setattr(backend_auth, "_probe_login_missing", lambda _backend: True)
    backend_auth._login_state_cache.clear()
    assert (
        failed_turn_login_backend(turn, CLAUDE_CODE_BACKEND, check_login=True)
        == CLAUDE_CODE_BACKEND
    )
    # A turn that did not fail is never blamed on the login.
    ok = {"turnId": "t4", "status": "completed"}
    assert failed_turn_login_backend(ok, CLAUDE_CODE_BACKEND, check_login=True) is None


def test_login_state_is_cached_briefly(monkeypatch) -> None:
    probes = []
    monkeypatch.setattr(
        backend_auth,
        "_probe_login_missing",
        lambda backend: probes.append(backend) or True,
    )
    assert backend_auth.backend_login_missing(CODEX_BACKEND)
    assert backend_auth.backend_login_missing(CODEX_BACKEND)
    assert probes == [CODEX_BACKEND]
    assert backend_auth.backend_login_missing(CODEX_BACKEND, fresh=True)
    assert probes == [CODEX_BACKEND, CODEX_BACKEND]
    assert not backend_auth.backend_login_missing(OPENBASE_CLOUD_BACKEND)


def test_messages_name_the_computer_and_the_fix() -> None:
    assert backend_login_message(CLAUDE_CODE_BACKEND) == (
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


def test_thread_chat_replaces_claude_notice_and_explains_codex(monkeypatch) -> None:
    from openbase_coder_cli.thread_sync.thread_messages import turn_messages

    messages = turn_messages(CLAUDE_TURN)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[-1]["text"] == backend_login_message(CLAUDE_CODE_BACKEND)

    monkeypatch.setattr(backend_auth, "_configured_backends", lambda: [CODEX_BACKEND])
    messages = turn_messages(CODEX_TURN)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[-1]["text"] == backend_login_message(CODEX_BACKEND)


def test_thread_output_is_the_login_message() -> None:
    from openbase_coder_cli.thread_sync.thread_payloads import _extract_agent_output

    assert _extract_agent_output(CLAUDE_TURN) == backend_login_message(
        CLAUDE_CODE_BACKEND
    )
    unclassified = {**CLAUDE_TURN, "errorKind": None, "status": "completed"}
    assert _extract_agent_output(unclassified) == "Not logged in · Please run /login"


def test_failed_turn_socket_error_is_the_login_message() -> None:
    from openbase_coder_cli.thread_sync.session_manager import _turn_failure_message

    params = {
        "threadId": "s1",
        "turnId": "t1",
        "error": {
            "message": CLAUDE_TURN["lastError"],
            "errorKind": "authentication_failed",
        },
    }
    assert _turn_failure_message(params) == backend_login_message(CLAUDE_CODE_BACKEND)
    assert _turn_failure_message({"error": {"message": "boom"}}) == "boom"
    assert login_failure_message({"error": {"message": "boom"}}) is None


@pytest.mark.parametrize(
    ("backend", "turn"),
    [(CLAUDE_CODE_BACKEND, CLAUDE_TURN), (CODEX_BACKEND, CODEX_TURN)],
)
def test_voice_speaks_login_failure(backend, turn) -> None:
    from openbase_coder_cli.livekit_agent import super_agents_client as client

    turn_id = turn.get("turnId") or turn["id"]
    progress = {"status": "failed", "turnId": turn_id, "turn": turn}
    login_backend = client._progress_login_backend(
        progress, backend=backend, turn_id=turn_id
    )
    assert login_backend == backend
    assert client._flag_backend_auth_failure(login_backend, backend=backend) is True
    spoken = client._safe_spoken_answer(
        "Not logged in · Please run /login", login_backend=login_backend
    )
    assert spoken == backend_login_spoken_message(backend)


@pytest.mark.asyncio
async def test_failed_voice_turn_without_error_kind_uses_login_check(
    tmp_path, monkeypatch
) -> None:
    """The reported bug: a Claude Code dispatcher turn fails with no speech;
    the CLI says it is logged out, so the call says how to sign in."""
    from openbase_coder_cli.livekit_agent import super_agents_client as client
    from tests.test_livekit_agent_super_agents_client import FakeSuperAgentsBackend

    monkeypatch.setattr(backend_auth, "_probe_login_missing", lambda _backend: True)
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


def test_cloud_workspace_message_points_at_the_ai_account_setting() -> None:
    assert backend_login_message(CODEX_BACKEND, cloud=True) == (
        "Codex isn't signed in on your cloud workspace. Relink it in "
        "Settings → AI Account, or switch back to Openbase Cloud."
    )
    spoken = backend_login_spoken_message(CLAUDE_CODE_BACKEND, cloud=True)
    assert spoken.startswith("Claude Code isn't signed in on your cloud workspace.")
    assert "→" not in spoken


def test_cloud_wording_follows_the_workspace(monkeypatch) -> None:
    monkeypatch.setattr(backend_auth, "on_cloud_workspace", lambda: True)
    assert "cloud workspace" in backend_login_message(CLAUDE_CODE_BACKEND)


def test_live_login_failure_marks_relink_until_cleared(monkeypatch) -> None:
    from openbase_coder_cli.thread_sync.session_manager import _turn_failure_message

    monkeypatch.setattr(backend_auth, "_configured_backends", lambda: [CODEX_BACKEND])
    assert backend_auth.relink_needed_backends() == set()
    _turn_failure_message({"error": CODEX_ERROR})
    assert backend_auth.relink_needed_backends() == {CODEX_BACKEND}
    backend_auth.clear_relink_needed(CODEX_BACKEND)
    assert backend_auth.relink_needed_backends() == set()


def test_rendering_history_does_not_mark_relink(monkeypatch) -> None:
    monkeypatch.setattr(backend_auth, "_configured_backends", lambda: [CODEX_BACKEND])
    assert login_failure_message(CODEX_TURN) == backend_login_message(CODEX_BACKEND)
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
