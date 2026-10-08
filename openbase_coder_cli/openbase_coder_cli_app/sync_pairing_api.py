"""Openbase Sync pairing API for the console Sync page.

Thin views over ``openbase_coder_cli.sync_pairing``, which the
``openbase-coder sync-daemon pair`` commands use too. ``pairing/offer/`` is
the one route another computer calls: the hub answers it, with the owner JWT
every desktop of the same account accepts, and it is the only response that
carries the pair secret.
"""

from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import sync_pairing


def _data(request) -> dict[str, Any]:
    return request.data if isinstance(request.data, dict) else {}


def _error(exc: sync_pairing.PairingError) -> Response:
    return Response(exc.to_dict(), status=exc.http_status)


def _roots_param(data: dict[str, Any]) -> list[str] | None | Response:
    roots = data.get("roots")
    if roots is None:
        return None
    if not isinstance(roots, list) or not all(
        isinstance(root, str) and root.strip() for root in roots
    ):
        return Response(
            {"error": "roots must be a list of folder paths", "code": "bad_roots"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return roots or None


@api_view(["GET"])
def sync_pairing_candidates(request):
    return Response(sync_pairing.candidates())


@api_view(["POST"])
def sync_pairing_hub(request):
    roots = _roots_param(_data(request))
    if isinstance(roots, Response):
        return roots
    try:
        result = sync_pairing.become_hub(roots)
    except sync_pairing.PairingError as exc:
        return _error(exc)
    sync_pairing.refresh_cloud_registration()
    return Response(result)


@api_view(["POST"])
def sync_pairing_join(request):
    data = _data(request)
    hub = data.get("hub")
    if not isinstance(hub, str) or not hub.strip():
        return Response(
            {"error": "Choose the computer to sync with.", "code": "hub_required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    roots = _roots_param(data)
    if isinstance(roots, Response):
        return roots
    try:
        result = sync_pairing.join_hub(hub.strip(), roots)
    except sync_pairing.PairingError as exc:
        return _error(exc)
    sync_pairing.refresh_cloud_registration()
    return Response(result)


@api_view(["POST"])
def sync_pairing_leave(request):
    try:
        result = sync_pairing.leave()
    except sync_pairing.PairingError as exc:
        return _error(exc)
    sync_pairing.refresh_cloud_registration()
    return Response(result)


@api_view(["POST"])
def sync_pairing_offer(request):
    """Answered by the hub only; the response carries the pair secret."""
    try:
        return Response(sync_pairing.offer())
    except sync_pairing.PairingError as exc:
        return _error(exc)


@api_view(["GET", "POST", "DELETE"])
def sync_daemon_roots(request):
    if request.method == "GET":
        return Response(sync_pairing.list_roots())
    data = _data(request)
    path = data.get("path") or request.query_params.get("path") or ""
    local_only = data.get("local_only") is True
    try:
        if request.method == "POST":
            result = sync_pairing.add_root(str(path), local_only=local_only)
        else:
            result = sync_pairing.remove_root(str(path), local_only=local_only)
    except sync_pairing.PairingError as exc:
        return _error(exc)
    return Response(result)
