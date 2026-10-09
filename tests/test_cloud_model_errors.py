import json

import pytest

from openbase_coder_cli.claude_auth import is_backend_auth_failure_text
from openbase_coder_cli.cloud_model_errors import (
    model_plan_denial_message,
    normalize_model_plan_error,
)
from openbase_coder_cli.thread_sync.thread_payloads import _run_from_turn


@pytest.mark.parametrize(
    "url", ["https://app-staging.openbase.cloud", "https://app.openbase.cloud"]
)
@pytest.mark.parametrize(
    "payload",
    [
        {
            "code": "model_not_available_on_plan",
            "detail": "This model is not available on your plan.",
        },
        {
            "detail": "Model 'claude-opus-4-8' is not available on the free or trial plan; Claude Haiku is. Subscribe at app.openbase.cloud."
        },
    ],
)
def test_denial_is_presented_as_plan_error_in_thread_history_and_voice(
    monkeypatch, url, payload
):
    from openbase_coder_cli.livekit_agent.super_agents_client import _safe_spoken_answer

    monkeypatch.setenv("OPENBASE_CODER_CLI_WEB_BACKEND_URL", url)
    raw = "Failed to authenticate. API Error: 403 " + json.dumps(payload)
    expected = f"This model is not available on your plan. Choose Claude Haiku or Sonnet, or upgrade at {url}."
    assert normalize_model_plan_error(raw) == expected
    assert not is_backend_auth_failure_text(raw)
    run = _run_from_turn(
        {
            "id": "turn-1",
            "status": "failed",
            "items": [{"type": "agentMessage", "text": raw}],
        }
    )
    assert run.accumulated_output == expected
    assert run.return_code == -1
    assert (
        _safe_spoken_answer(raw, auth_failed=False, backend="openbase_cloud")
        == expected
    )


@pytest.mark.parametrize(
    "raw",
    [
        'Failed to authenticate. API Error: 403 {"detail":"Invalid credentials"}',
        'Failed to authenticate. API Error: 401 {"code":"model_not_available_on_plan"}',
        "Failed to authenticate. API Error: 403 not-json",
        'Here is an example: API Error: 403 {"code":"model_not_available_on_plan"}',
        'API Error: 403 {"detail":"Monthly Openbase model spend limit reached"}',
        "391",
    ],
)
def test_unrelated_errors_and_normal_answers_are_unchanged(raw):
    assert model_plan_denial_message(raw) is None
    assert normalize_model_plan_error(raw) == raw
    if raw.startswith("Failed to authenticate"):
        assert is_backend_auth_failure_text(raw)
