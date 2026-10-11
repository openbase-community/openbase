"""Self-update API views (see the workspace AUTO_UPDATE.md guide)."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.self_update import (
    SELF_UPDATE_LOG_PATH,
    SelfUpdateError,
    check_for_update,
    spawn_detached_self_update,
    updatable_runtime_package,
    version_info,
)


@api_view(["GET"])
def update_status(request):
    """Version facts plus update flags; ?refresh=1 re-checks the feed."""
    payload = version_info()
    if request.query_params.get("refresh"):
        try:
            check = check_for_update()
        except SelfUpdateError as exc:
            payload["check_error"] = str(exc)
        else:
            payload["update_available"] = check.update_available
            payload["update_required"] = check.update_required
            if check.latest_version:
                payload["latest_version"] = check.latest_version
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["POST"])
def update_apply(request):
    """Kick off a detached self-update of the standalone install.

    Detached because the update restarts the very services serving this
    request; progress lands in the self-update log.
    """
    if updatable_runtime_package() is None:
        return Response(
            {
                "error": "Development workspace installs are git-managed; no auto-update."
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        spawn_detached_self_update(force=bool(request.data.get("force")))
    except SelfUpdateError as exc:
        return Response(
            {"error": str(exc)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return Response(
        {"started": True, "log": str(SELF_UPDATE_LOG_PATH)},
        status=status.HTTP_202_ACCEPTED,
    )
