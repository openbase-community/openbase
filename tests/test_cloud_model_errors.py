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
    expected = f"This model is not available on your plan. Subscribe to a Pro plan on {url.removeprefix('https://')} for expanded access."
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
    expected = f"Your monthly Openbase model allowance is used up. Subscribe to a Pro plan on {url.removeprefix('https://')} for expanded access."
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
    # The voice agent says one short sentence; the thread keeps the full text.
    assert (
        _safe_spoken_answer(raw, auth_failed=False)
        == "You're out of Openbase model credits. Subscribe to a Pro plan on app dot openbase dot cloud for expanded access."
    )
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


@pytest.mark.parametrize(
    "raw",
    [
        "Credit balance is too low",
        'API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."}}',
    ],
)
def test_provider_credit_exhaustion_reads_as_a_temporary_outage(raw):
    # The platform's provider account, not the user's allowance: the user
    # cannot act on it, so neither the raw text nor an upgrade prompt fits.
    from openbase_coder_cli.cloud_model_errors import (
        PROVIDER_UNAVAILABLE_MESSAGE,
        model_proxy_denial_spoken_message,
        normalize_model_proxy_error,
    )

    assert normalize_model_proxy_error(raw) == PROVIDER_UNAVAILABLE_MESSAGE
    assert model_proxy_denial_spoken_message(raw) == PROVIDER_UNAVAILABLE_MESSAGE
    assert "credit" not in PROVIDER_UNAVAILABLE_MESSAGE.lower()


@pytest.mark.parametrize(
    "prefix", ["API Error: 403 ", "Failed to authenticate. API Error: 403 "]
)
def test_request_budget_error_does_not_claim_monthly_exhaustion(prefix):
    from openbase_coder_cli.claude_auth import is_spend_limit_text
    from openbase_coder_cli.cloud_model_errors import model_proxy_denial_spoken_message
    from openbase_coder_cli.livekit_agent.super_agents_client import _safe_spoken_answer

    raw = prefix + json.dumps(
        {
            "code": "model_request_exceeds_allowance",
            "detail": "This model request's estimated cost exceeds the remaining allowance.",
            "remaining_millicents": "83463",
        }
    )
    expected = "This request may cost more than your remaining model credit. Start a shorter conversation or ask for a shorter reply."
    assert normalize_model_proxy_error(raw) == expected
    assert model_proxy_denial_spoken_message(raw) == expected
    assert (
        _safe_spoken_answer(raw, auth_failed=False, backend="openbase_cloud")
        == expected
    )
    assert not is_backend_auth_failure_text(raw)
    assert is_spend_limit_text(raw)
    run = _run_from_turn({"id": "turn-1", "status": "failed", "lastUsefulMessage": raw})
    assert run.accumulated_output == expected
    assert "used up" not in expected
    assert (
        _safe_spoken_answer(raw, auth_failed=True, backend="openbase_cloud") == expected
    )
    from openbase_coder_cli.livekit_agent import turn_recovery

    failure = turn_recovery.classify_error_text(raw)
    assert failure.kind == turn_recovery.KIND_TERMINAL
    assert not failure.retryable


@pytest.mark.parametrize(
    "raw",
    [
        'API Error: 401 {"code":"model_request_exceeds_allowance"}',
        'API Error: 500 {"code":"model_request_exceeds_allowance"}',
        'Yesterday I saw API Error: 403 {"code":"model_request_exceeds_allowance"}',
    ],
)
def test_request_budget_marker_only_matches_proxy_denials(raw):
    assert normalize_model_proxy_error(raw) == raw


def test_production_line_is_gabes_exact_wording():
    from openbase_coder_cli.plan_copy import SUBSCRIBE_PRO_LINE, subscribe_pro_line

    assert SUBSCRIBE_PRO_LINE == (
        "Subscribe to a Pro plan on app.openbase.cloud for expanded access."
    )
    assert subscribe_pro_line("https://app.openbase.cloud") == SUBSCRIBE_PRO_LINE
