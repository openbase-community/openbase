"""Present proxy plan denials without the Claude SDK's authentication prefix."""

from __future__ import annotations

import json

from openbase_coder_cli.cloud_environment import configured_web_backend_url


def model_plan_denial_message(text: str | None) -> str | None:
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
    detail = payload.get("detail")
    legacy_denial = (
        isinstance(detail, str)
        and "is not available on the free or trial plan" in detail
    )
    if payload.get("code") != "model_not_available_on_plan" and not legacy_denial:
        return None
    return (
        "This model is not available on your plan. Choose Claude Haiku or Sonnet, "
        f"or upgrade at {configured_web_backend_url()}."
    )


def normalize_model_plan_error(text: str) -> str:
    return model_plan_denial_message(text) or text
