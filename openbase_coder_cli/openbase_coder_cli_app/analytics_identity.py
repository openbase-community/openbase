"""Analytics identity proxy for clients that only reach the local backend.

The desktop app talks exclusively to this local server, so its Amplitude
device id can only be bound to the signed-in Openbase account by relaying
``POST /api/openbase/analytics/identify`` to Openbase Cloud with the CLI's own
cloud credentials. Without this route the desktop identify call 404s silently
and desktop events never join the account's analytics identity.
"""

from __future__ import annotations

import httpx
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
    get_token_manager,
)
from openbase_coder_cli.openbase_coder_cli_app.common import offloaded_view
from openbase_coder_cli.services.onboarding import web_backend_url

CLOUD_IDENTIFY_PATH = "/api/openbase/analytics/identify/"
REQUEST_TIMEOUT_SECONDS = 15
MAX_IDENTIFIER_LENGTH = 255

# Mirrors the cloud ``IdentifyRequestSerializer``: anonymous client ids plus
# optional first-touch channel fields. Anything else never leaves the machine.
FORWARDED_FIELDS = frozenset(
    {
        "ga_client_id",
        "amplitude_device_id",
        "attribution_source",
        "attribution_medium",
        "attribution_campaign",
        "attribution_referrer_domain",
        "attribution_landing_path",
    }
)


def identify_payload(data: object) -> dict[str, str]:
    """Keep only the cloud-accepted string fields of a client identify body."""
    if not isinstance(data, dict):
        return {}
    payload: dict[str, str] = {}
    for key, value in data.items():
        if key not in FORWARDED_FIELDS or not isinstance(value, str):
            continue
        trimmed = value.strip()
        if trimmed:
            payload[key] = trimmed[:MAX_IDENTIFIER_LENGTH]
    return payload


@offloaded_view
@api_view(["POST"])
def analytics_identify(request):
    """Relay an analytics identify call to Openbase Cloud for the signed-in user.

    Returns the cloud response body (``analytics_key`` plus a message) so the
    caller can adopt the account's stable analytics key as its ``user_id``.
    """
    payload = identify_payload(request.data)
    if not payload:
        return Response(
            {"error": "No analytics identifiers to link."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    backend_url = web_backend_url()
    try:
        token = get_token_manager(backend_url).get_access_token()
        response = httpx.post(
            f"{backend_url}{CLOUD_IDENTIFY_PATH}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except AuthLoginRequiredError as exc:
        return Response(
            {"error": str(exc) or "Openbase login required."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    except (AuthTransientError, httpx.HTTPError) as exc:
        return Response(
            {"error": f"Openbase Cloud unreachable: {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    if response.status_code == 401:
        return Response(
            {"error": "Openbase login required."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    if response.status_code >= 400:
        return Response(
            {
                "error": f"Openbase Cloud rejected the identify request ({response.status_code})."
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return Response(
        {
            "analytics_key": body.get("analytics_key"),
            "message": body.get("message", ""),
        },
        status=status.HTTP_200_OK,
    )
