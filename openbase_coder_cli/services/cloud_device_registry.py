"""Read the signed-in user's device registry from Openbase Cloud."""

from __future__ import annotations

from typing import Any

import httpx

from openbase_coder_cli.config.token_manager import TokenManager
from openbase_coder_cli.services.onboarding import web_backend_url

ONBOARDING_STATE_PATH = "/api/openbase/onboarding/state/"
REQUEST_TIMEOUT_SECONDS = 15


def fetch_cloud_state() -> dict[str, Any]:
    """GET the signed-in user's registered devices. Raises on failure."""
    backend_url = web_backend_url()
    token = TokenManager(backend_url).get_access_token()
    response = httpx.get(
        f"{backend_url}{ONBOARDING_STATE_PATH}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Onboarding state endpoint returned a non-object.")
    return payload
