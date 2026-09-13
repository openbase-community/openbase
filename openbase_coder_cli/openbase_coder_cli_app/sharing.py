"""Report sharing API views: local surface over the cloud sharing service.

The console and apps talk to these device-local endpoints; the runtime
relays to Openbase Cloud over the signed-in user's session. Sharing is
account-gated per email grant — there are no public links.
"""

from __future__ import annotations

from pathlib import Path

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import sharing_service
from openbase_coder_cli.services import cloud_sharing


def _share_target(request):
    project_path = (
        request.query_params.get("path") or request.data.get("path") or ""
    ).strip()
    relative_path = (
        request.query_params.get("file") or request.data.get("file") or ""
    ).strip()
    if not project_path:
        return (
            None,
            None,
            Response({"error": "path is required"}, status=status.HTTP_400_BAD_REQUEST),
        )
    if not relative_path:
        return (
            None,
            None,
            Response({"error": "file is required"}, status=status.HTTP_400_BAD_REQUEST),
        )
    resolved = Path(project_path).expanduser().resolve()
    if not resolved.is_dir():
        return (
            None,
            None,
            Response(
                {"error": f"Directory not found: {resolved}"},
                status=status.HTTP_400_BAD_REQUEST,
            ),
        )
    return str(resolved), relative_path, None


@api_view(["GET", "POST", "DELETE"])
def report_share(request):
    """Share state (GET), publish/refresh a share (POST), unshare (DELETE)."""
    project_path, relative_path, error = _share_target(request)
    if error is not None:
        return error

    if request.method == "GET":
        return Response(sharing_service.get_share_state(project_path, relative_path))

    if request.method == "DELETE":
        result = sharing_service.unshare_report(project_path, relative_path)
        if not result.get("ok"):
            return Response(
                {"error": result.get("error") or "Unable to unshare"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"shared": False})

    allow_secrets = bool(request.data.get("confirm_secrets"))
    try:
        result = sharing_service.publish_report(
            project_path, relative_path, allow_secrets=allow_secrets
        )
    except FileNotFoundError:
        return Response(
            {"error": f"File not found: {relative_path}"},
            status=status.HTTP_404_NOT_FOUND,
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    if result.get("reason") == "possible_secrets":
        return Response(
            {
                "error": "This report appears to contain secrets.",
                "reason": "possible_secrets",
                "findings": result.get("findings") or [],
            },
            status=status.HTTP_409_CONFLICT,
        )
    if not result.get("ok"):
        return Response(
            {"error": result.get("error") or "Unable to publish share"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response(sharing_service.get_share_state(project_path, relative_path))


@api_view(["POST"])
def report_share_grants(request):
    """Grant a person access by email; the cloud emails them a view link."""
    project_path, relative_path, error = _share_target(request)
    if error is not None:
        return error
    email = (request.data.get("email") or "").strip()
    if not email:
        return Response(
            {"error": "email is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    state = sharing_service.get_share_state(project_path, relative_path)
    if not state.get("shared"):
        return Response(
            {"error": "Report is not shared yet"},
            status=status.HTTP_409_CONFLICT,
        )

    result = cloud_sharing.add_grant(state["item"]["id"], email)
    if not result.ok:
        message = result.error or "Unable to add grant"
        response_status = (
            status.HTTP_400_BAD_REQUEST
            if result.status_code == 400
            else status.HTTP_502_BAD_GATEWAY
        )
        return Response({"error": message}, status=response_status)
    return Response(sharing_service.get_share_state(project_path, relative_path))


@api_view(["POST"])
def report_share_revoke_grant(request):
    """Revoke one grant on this report's share."""
    project_path, relative_path, error = _share_target(request)
    if error is not None:
        return error
    grant_id = (request.data.get("grant_id") or "").strip()
    if not grant_id:
        return Response(
            {"error": "grant_id is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    state = sharing_service.get_share_state(project_path, relative_path)
    if not state.get("shared"):
        return Response(
            {"error": "Report is not shared"}, status=status.HTTP_409_CONFLICT
        )

    result = cloud_sharing.revoke_grant(state["item"]["id"], grant_id)
    if not result.ok:
        return Response(
            {"error": result.error or "Unable to revoke grant"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response(sharing_service.get_share_state(project_path, relative_path))
