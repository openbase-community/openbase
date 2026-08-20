"""Onboarding status API views."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from openbase_coder_cli.services.onboarding import onboarding_status_payload

# AllowAny: the desktop onboarding shell polls these endpoints anonymously —
# they are how the UI learns the user just signed in, so they cannot sit
# behind the local JWT they help bootstrap. Localhost callers can already
# mint a full JWT via the AllowAny /api/auth/refresh-jwt/ endpoint, so this
# exposes nothing a local process could not already read.


@api_view(["GET"])
@permission_classes([AllowAny])
def onboarding_status(request):
    """Report local onboarding state (CLI configured, Tailscale, auth)."""
    return Response(onboarding_status_payload(), status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([AllowAny])
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
