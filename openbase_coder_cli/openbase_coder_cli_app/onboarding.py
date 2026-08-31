"""Onboarding status API views."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.services.onboarding import onboarding_status_payload


@api_view(["GET"])
def onboarding_status(request):
    """Report local onboarding state (CLI configured, Tailscale, auth)."""
    return Response(onboarding_status_payload(), status=status.HTTP_200_OK)


@api_view(["GET"])
def onboarding_cloud_state(request):
    """Live cloud device-registry state for the signed-in user.

    Proxies ``GET /api/openbase/onboarding/state/`` through the CLI token
    manager on every request. The cloud registry is the source of truth for
    pairing facts, so nothing here is cached — unlike the ``cloud`` block in
    the status payload, which is only a last-report hint.
    """
    import httpx

    from openbase_coder_cli.code_sync import eligibility
    from openbase_coder_cli.config.token_manager import (
        AuthLoginRequiredError,
        AuthTransientError,
    )

    try:
        state = eligibility.fetch_cloud_state()
    except AuthLoginRequiredError as exc:
        return Response(
            {"error": str(exc) or "Openbase login required."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    except (AuthTransientError, httpx.HTTPError, ValueError) as exc:
        return Response(
            {"error": f"Cloud device registry unreachable: {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response(state, status=status.HTTP_200_OK)
