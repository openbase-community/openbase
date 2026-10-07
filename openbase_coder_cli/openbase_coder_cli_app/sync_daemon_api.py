"""Openbase Sync daemon API: thin proxies to the daemon's control socket.

These routes serve the console Sync page and the apps while the Syncthing
routes under ``/api/sync/`` are still present; the daemon owns all state.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import sync_daemon


def _client() -> sync_daemon.SyncDaemonClient:
    return sync_daemon.SyncDaemonClient()


def _unavailable(exc: Exception) -> Response:
    return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


@api_view(["GET"])
def sync_daemon_settings(request):
    """Configuration summary plus whether the daemon answers."""
    summary = sync_daemon.read_config_summary()
    summary["reachable"] = summary["configured"] and sync_daemon.reachable()
    return Response(summary)


@api_view(["GET"])
def sync_daemon_status(request):
    try:
        payload = _client().status()
        payload["metrics"] = _client().metrics()
    except sync_daemon.SyncDaemonError as exc:
        return _unavailable(exc)
    return Response(payload)


@api_view(["GET"])
def sync_daemon_conflicts(request):
    try:
        conflicts = _client().conflicts(request.query_params.get("root") or None)
    except sync_daemon.SyncDaemonError as exc:
        return _unavailable(exc)
    return Response({"conflicts": conflicts, "unresolved_count": len(conflicts)})


@api_view(["POST"])
def sync_daemon_conflicts_resolve(request):
    data = request.data if isinstance(request.data, dict) else {}
    conflict_id = data.get("id")
    action = data.get("action")
    choice = {
        "keep_local": "a",
        "keep_mine": "a",
        "a": "a",
        "use_remote": "b",
        "take_theirs": "b",
        "b": "b",
    }.get(action)
    if conflict_id is None or choice is None:
        return Response(
            {"error": "id and action (keep_local|use_remote) are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        _client().resolve(int(conflict_id), choice)
    except (sync_daemon.SyncDaemonError, ValueError) as exc:
        return _unavailable(exc)
    return Response({"resolved": True, "id": int(conflict_id), "choice": choice})


@api_view(["POST"])
def sync_daemon_barrier(request):
    data = request.data if isinstance(request.data, dict) else {}
    kind = data.get("kind") or "settle"
    if kind not in {"flush", "settle", "path"}:
        return Response(
            {"error": "kind must be flush, settle or path"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        result = _client().barrier(
            kind,
            path=data.get("path"),
            root=data.get("root"),
            timeout_ms=int(data.get("timeout_ms") or 300),
        )
    except sync_daemon.SyncDaemonError as exc:
        return _unavailable(exc)
    return Response(result)


@api_view(["GET"])
def sync_daemon_stubs(request):
    try:
        stubs = _client().stubs(request.query_params.get("root") or None)
    except sync_daemon.SyncDaemonError as exc:
        return _unavailable(exc)
    return Response({"stubs": stubs})


@api_view(["POST"])
def sync_daemon_hydrate(request):
    data = request.data if isinstance(request.data, dict) else {}
    path = data.get("path")
    if not path:
        return Response(
            {"error": "path is required"}, status=status.HTTP_400_BAD_REQUEST
        )
    try:
        result = _client().hydrate(str(path))
    except sync_daemon.SyncDaemonError as exc:
        return _unavailable(exc)
    return Response(result)
