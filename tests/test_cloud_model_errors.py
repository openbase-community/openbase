import json

import pytest

from openbase_coder_cli.claude_auth import is_backend_auth_failure_text
from openbase_coder_cli.cloud_model_errors import (
    model_plan_denial_message,
    normalize_model_proxy_error,
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
    expected = f"This model is not available on your plan. Choose Claude Haiku, or upgrade at {url}."
    assert normalize_model_proxy_error(raw) == expected
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
        "391",
    ],
)
def test_unrelated_errors_and_normal_answers_are_unchanged(raw):
    assert model_plan_denial_message(raw) is None
    assert normalize_model_proxy_error(raw) == raw
    if raw.startswith("Failed to authenticate"):
        assert is_backend_auth_failure_text(raw)


@pytest.mark.parametrize(
    "url", ["https://app-staging.openbase.cloud", "https://app.openbase.cloud"]
)
@pytest.mark.parametrize(
    "prefix", ["Failed to authenticate. API Error: 403 ", "API Error: 403 "]
)
def test_allowance_denial_in_history_voice_and_auth_classification(
    monkeypatch, url, prefix
):
    from openbase_coder_cli.claude_auth import is_spend_limit_text
    from openbase_coder_cli.livekit_agent.super_agents_client import _safe_spoken_answer

    monkeypatch.setenv("OPENBASE_CODER_CLI_WEB_BACKEND_URL", url)
    raw = prefix + json.dumps(
        {
            "detail": "Monthly Openbase model proxy spend limit reached. Model requests are blocked until next month. Subscribe at app.openbase.cloud to raise your monthly limits."
        }
    )
    expected = f"Your monthly Openbase model allowance is used up. Upgrade your plan at {url} to raise your monthly limit, or wait until your allowance resets next month."
    assert normalize_model_proxy_error(raw) == expected
    assert not is_backend_auth_failure_text(raw)
    assert is_spend_limit_text(raw)
    for turn in (
        {
            "id": "turn-1",
            "status": "failed",
            "items": [{"type": "agentMessage", "text": raw}],
        },
        {"id": "turn-1", "status": "failed", "lastUsefulMessage": raw},
    ):
        run = _run_from_turn(turn)
        assert run.accumulated_output == expected
        assert run.return_code == -1
    assert _safe_spoken_answer(raw, auth_failed=False) == expected
    assert "try again" not in expected


@pytest.mark.parametrize(
    "raw",
    [
        "I saw a monthly Openbase model proxy spend limit reached error yesterday.",
        'Failed to authenticate. API Error: 401 {"detail":"Monthly Openbase model proxy spend limit reached."}',
        'API Error: 500 {"detail":"Monthly Openbase model proxy spend limit reached."}',
        'API Error: 403 {"detail":"Invalid credentials. Raise your monthly limits."}',
    ],
)
def test_allowance_detection_does_not_reclassify_other_errors_or_prose(raw):
    from openbase_coder_cli.claude_auth import is_spend_limit_text

    assert not is_spend_limit_text(raw)
    assert normalize_model_proxy_error(raw) == raw
