"""Present proxy billing denials without the Claude SDK's authentication prefix."""

from __future__ import annotations

import json

from openbase_coder_cli.cloud_environment import configured_web_backend_url


def _proxy_denial_payload(text: str | None) -> dict | None:
    if not text:
        return None
    raw = text.strip()
    if not raw.startswith(("Failed to authenticate. API Error: 403", "API Error: 403")):
        return None
    start = raw.find("{")
    if start < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(raw[start:])
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def model_plan_denial_message(text: str | None) -> str | None:
    payload = _proxy_denial_payload(text)
    if payload is None:
        return None
    detail = payload.get("detail")
    legacy_denial = (
        isinstance(detail, str)
        and "is not available on the free or trial plan" in detail
    )
    if payload.get("code") != "model_not_available_on_plan" and not legacy_denial:
        return None
    return (
        "This model is not available on your plan. Choose Claude Haiku, "
        f"or upgrade at {configured_web_backend_url()}."
    )


def model_allowance_exhausted_message(text: str | None) -> str | None:
    payload = _proxy_denial_payload(text)
    if payload is None:
        return None
    detail = payload.get("detail")
    if not isinstance(detail, str) or not detail.lower().startswith(
        (
            "monthly openbase model proxy spend limit reached",
            "monthly openbase model spend limit reached",
        )
    ):
        return None
    return (
        "Your monthly Openbase model allowance is used up. "
        f"Upgrade your plan at {configured_web_backend_url()} to raise your monthly limit, "
        "or wait until your allowance resets next month."
    )


# One short, complete sentence for voice: a live call's speech gate cuts the
# rest of a reply as soon as the caller is heard, so the spoken form must not
# depend on a second clause to make sense. The thread keeps the full text.
MODEL_ALLOWANCE_EXHAUSTED_SPOKEN = (
    "This account's monthly Openbase model allowance is used up."
)


def model_allowance_exhausted_spoken_message(text: str | None) -> str | None:
    if model_allowance_exhausted_message(text) is None:
        return None
    return MODEL_ALLOWANCE_EXHAUSTED_SPOKEN


# The provider's own billing refusal (Anthropic: "Your credit balance is too
# low to access the Anthropic API"; Claude Code shows it as "Credit balance is
# too low"). It is the platform's provider account, never the user's: nothing
# the user can do about it, so it reads as a temporary outage.
PROVIDER_CREDIT_EXHAUSTED_MARKERS = ("credit balance is too low",)
PROVIDER_UNAVAILABLE_MESSAGE = (
    "Openbase Cloud's model provider is temporarily unavailable. "
    "Please try again in a few minutes."
)


def provider_credit_exhausted_message(text: str | None) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    if not any(marker in lowered for marker in PROVIDER_CREDIT_EXHAUSTED_MARKERS):
        return None
    return PROVIDER_UNAVAILABLE_MESSAGE


def model_proxy_denial_message(text: str | None) -> str | None:
    return (
        model_allowance_exhausted_message(text)
        or model_plan_denial_message(text)
        or provider_credit_exhausted_message(text)
    )


def model_proxy_denial_spoken_message(text: str | None) -> str | None:
    """The denial as the voice agent should say it."""
    return (
        model_allowance_exhausted_spoken_message(text)
        or model_plan_denial_message(text)
        or provider_credit_exhausted_message(text)
    )


def normalize_model_proxy_error(text: str) -> str:
    return model_proxy_denial_message(text) or text
