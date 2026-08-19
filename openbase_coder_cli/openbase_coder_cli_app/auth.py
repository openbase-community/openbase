"""Local auth/session API views."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    get_token_manager,
)

logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([AllowAny])
def auth_session(request):
    """Report validated Openbase Cloud login state for this install.

    ``logged_in`` means the cloud still accepts the stored credentials (or
    the cloud was unreachable and credentials are present — see
    ``validated``), not merely that a token file exists.
    """
    manager = get_token_manager()
    login = manager.login_status()
    return Response(
        {
            "logged_in": login["status"] == "logged_in",
            "status": login["status"],
            "validated": login["validated"],
            "detail": login["detail"],
            "auth_path": str(Path.home() / ".openbase" / "auth.json"),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def auth_refresh_jwt(request):
    """Return a fresh cloud JWT for the local console without exposing refresh tokens."""
    manager = get_token_manager()
    if not manager.has_refresh_token:
        return Response(
            {"detail": "Login required. Run 'openbase-coder login' first."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    try:
        payload = manager.get_access_token_payload()
    except AuthLoginRequiredError as exc:
        logger.exception("Cloud rejected the stored refresh token")
        return Response(
            {"detail": str(exc)},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    except Exception as exc:
        # Network errors and backend 5xx are retryable; a 401 here would
        # make the console treat the user as logged out.
        logger.exception("Unable to refresh local console JWT")
        return Response(
            {"detail": f"Unable to refresh JWT: {exc}"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    payload["expires_at"] = int(time.time()) + payload["access_token_expires_in"]
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([AllowAny])
def auth_logout(request):
    """Clear the locally stored JWT tokens."""
    from openbase_coder_cli.voice_lockdown import get_voice_lockdown_broker

    get_voice_lockdown_broker().revoke_all(reason="logout")
    get_token_manager().clear()
    return Response({"success": True}, status=status.HTTP_200_OK)
