"""Regression tests for backend-identity preservation when building the
LiveKit dispatcher's Super Agents client.

An openbase_cloud install must hand its client the *configured* backend
(openbase_cloud), not the resolved execution engine (claude_code), or the
Claude SDK loses its Openbase Cloud proxy env + model mapping and answers every
turn "Not logged in · Please run /login." (FT-DISPATCH-011).
"""

from __future__ import annotations

from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)
from openbase_coder_cli.livekit_agent.super_agents_client_threads import (
    _client_backend_identity,
)


def test_openbase_cloud_keeps_cloud_identity_for_claude_execution() -> None:
    # The bug: openbase_cloud maps to claude_code execution, and dropping the
    # identity to claude_code disables the Cloud Anthropic proxy.
    assert (
        _client_backend_identity(OPENBASE_CLOUD_BACKEND, CLAUDE_CODE_BACKEND)
        == OPENBASE_CLOUD_BACKEND
    )


def test_openbase_cloud_codex_keeps_cloud_identity_for_codex_execution() -> None:
    assert (
        _client_backend_identity(OPENBASE_CLOUD_CODEX_BACKEND, CODEX_BACKEND)
        == OPENBASE_CLOUD_CODEX_BACKEND
    )


def test_personal_backends_keep_their_own_identity() -> None:
    # A personal claude_code / codex login must NOT be treated as cloud-proxied.
    assert (
        _client_backend_identity(CLAUDE_CODE_BACKEND, CLAUDE_CODE_BACKEND)
        == CLAUDE_CODE_BACKEND
    )
    assert _client_backend_identity(CODEX_BACKEND, CODEX_BACKEND) == CODEX_BACKEND


def test_model_override_to_other_engine_falls_back_to_execution_backend() -> None:
    # Incoherent combo: openbase_cloud (Claude) configured, but a per-role
    # dispatcher model forced the Codex engine. Do not claim a cloud-codex
    # identity that wasn't configured; use the plain execution backend.
    assert (
        _client_backend_identity(OPENBASE_CLOUD_BACKEND, CODEX_BACKEND) == CODEX_BACKEND
    )
    # And the mirror: codex configured but a Claude model was chosen.
    assert (
        _client_backend_identity(CODEX_BACKEND, CLAUDE_CODE_BACKEND)
        == CLAUDE_CODE_BACKEND
    )
