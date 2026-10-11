"""Local auth/session API views."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from openbase_coder_cli.config.token_manager import get_token_manager


@api_view(["POST"])
@permission_classes([AllowAny])
def auth_refresh_jwt_removed(request):
    """Tombstone the former owner-token oracle without touching credentials."""
    return Response(
        {"detail": "This credential-minting endpoint has been removed."},
        status=status.HTTP_410_GONE,
    )


@api_view(["GET"])
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
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
def auth_logout(request):
    """Sign this machine out of the account, then clear the stored tokens.

    The desktop app and console sign out through here, so this must release
    the account's VPN node and device record exactly like the CLI logout.
    """
    from openbase_coder_cli.services.netmesh_account import sign_out_account

    manager = get_token_manager()
    if manager.has_refresh_token:
        sign_out_account()
    manager.clear()
    return Response({"success": True}, status=status.HTTP_200_OK)
