"""Cloud-authoritative model availability for the installed account."""

from __future__ import annotations

from super_agents.claude_options import OPENBASE_CLOUD_CLAUDE_MODEL_MAP

from openbase_coder_cli.cloud_environment import configured_web_backend_url
from openbase_coder_cli.config.cloud_audio import (
    OpenbaseCloudAudioSubscriptionError,
    _cloud_json_get,
)
from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
)

CATALOG_UNAVAILABLE_REASON = "Could not verify model availability with Openbase Cloud. Refresh the model list to try again."


def cloud_model_availability() -> dict[str, str | None]:
    """Map SDK aliases to a denial reason, or None when explicitly available.

    Fetch fresh account policy on each catalog request so an upgrade or account
    change takes effect immediately. Unknown/older policy is unavailable until
    it can be verified, rather than advertising paid models to trial accounts.
    """
    try:
        payload = _cloud_json_get(
            configured_web_backend_url(), "/api/openbase/llm/anthropic/v1/models/"
        )
    except (
        AuthLoginRequiredError,
        AuthTransientError,
        OpenbaseCloudAudioSubscriptionError,
    ):
        return dict.fromkeys(
            OPENBASE_CLOUD_CLAUDE_MODEL_MAP, CATALOG_UNAVAILABLE_REASON
        )
    entries = payload.get("data")
    models = (
        {
            entry["id"]: entry
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }
        if isinstance(entries, list)
        else {}
    )
    result = {}
    for alias, model_id in OPENBASE_CLOUD_CLAUDE_MODEL_MAP.items():
        entry = models.get(model_id, {})
        reason = entry.get("unavailable_reason")
        result[alias] = (
            None
            if entry.get("available") is True
            else (
                reason
                if isinstance(reason, str) and reason.strip()
                else CATALOG_UNAVAILABLE_REASON
            )
        )
    return result
