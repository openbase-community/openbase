"""Openbase Sync daemon API: thin proxies to the daemon's control socket.

``/api/sync/daemon/*`` serves the console Sync page. ``/api/sync/status/``,
``/api/sync/conflicts/`` and ``/api/sync/conflicts/resolve/`` keep the
response shapes the phone apps already decode, now backed by the daemon.
The daemon owns all state; nothing here stores any.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
        conflict_id = int(conflict_id)
    except (TypeError, ValueError):
        return Response(
            {"error": f"Unknown conflict id {conflict_id!r}."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        _client().resolve(conflict_id, choice)
    except sync_daemon.SyncDaemonError as exc:
        return _unavailable(exc)
    return Response({"resolved": True, "id": conflict_id, "choice": choice})


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


# --- Compatibility routes for the phone apps ---------------------------------


def _display_relpath(path: str) -> str:
    """``~/Projects`` -> ``Projects``; absolute paths outside home unchanged."""
    if not path:
        return ""
    if path == "~":
        return "."
    if path.startswith("~/"):
        return path[2:]
    try:
        return str(Path(path).relative_to(Path.home()))
    except ValueError:
        return path


def _peer_completion(root: dict[str, Any], peers: list[dict[str, Any]]) -> dict:
    seq = int(root.get("seq") or 0)
    completion: dict[str, float] = {}
    for peer in peers:
        acked = int(
            ((peer.get("roots") or {}).get(root.get("id")) or {}).get("acked_seq") or 0
        )
        percent = 100.0 if seq <= 0 or acked >= seq else round(100.0 * acked / seq, 1)
        completion[str(peer.get("device") or "")] = percent
    return completion


def _legacy_folder(root: dict[str, Any], peers: list[dict[str, Any]]) -> dict:
    pending = int(root.get("pending_fetches") or 0)
    scanning = bool(root.get("scanning"))
    peer_completion = _peer_completion(root, peers)
    local = 100.0 if not pending and not scanning else 0.0
    completion = min([local, *peer_completion.values()])
    return {
        "id": str(root.get("id") or ""),
        "relpath": _display_relpath(str(root.get("path") or "")),
        "state": "scanning" if scanning else ("syncing" if pending else "idle"),
        "completion": completion,
        "receive_only": False,
        "peer_completion": peer_completion,
        "error": "",
    }


@api_view(["GET"])
def sync_status(request):
    """Per-root sync health in the shape the phone apps decode."""
    if not sync_daemon.is_configured():
        return Response(
            {
                "enabled": False,
                "folders": [],
                "last_reconcile_at": None,
                "conflicts_count": 0,
            }
        )
    try:
        payload = _client().status()
    except sync_daemon.SyncDaemonError as exc:
        folders = [
            {
                "id": root.get("id", ""),
                "relpath": _display_relpath(root.get("path", "")),
                "state": "unreachable",
                "completion": None,
                "receive_only": False,
                "peer_completion": {},
                "error": str(exc),
            }
            for root in sync_daemon.configured_roots()
        ]
        return Response(
            {
                "enabled": True,
                "folders": folders,
                "last_reconcile_at": None,
                "conflicts_count": 0,
                "error": str(exc),
            }
        )
    peers = [peer for peer in payload.get("peers") or [] if isinstance(peer, dict)]
    return Response(
        {
            "enabled": True,
            "role": payload.get("role") or "",
            "folders": [
                _legacy_folder(root, peers)
                for root in payload.get("roots") or []
                if isinstance(root, dict)
            ],
            "peers_connected": len(peers),
            "last_reconcile_at": None,
            "conflicts_count": int(payload.get("open_conflicts") or 0),
        }
    )


def _iso_from_ns(value: Any) -> str:
    try:
        moment = datetime.fromtimestamp(int(value) / 1e9, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _legacy_conflict(conflict: dict[str, Any], root_paths: dict[str, str]) -> dict:
    kind = str(conflict.get("kind") or "")
    path = str(conflict.get("path") or "")
    root_id = str(conflict.get("root") or "")
    base = {
        "id": str(conflict.get("id")),
        "kind": kind,
        "folder_id": root_id,
        "folder_relpath": _display_relpath(root_paths.get(root_id, "")),
        "detected_at": _iso_from_ns(conflict.get("created_ns")),
        "resolved": False,
        "conflict_device_hint": str(conflict.get("b_device") or ""),
        "label": str(conflict.get("label") or ""),
    }
    if kind == "git-branch":
        return {
            **base,
            "type": "repo-divergence",
            "repo_relpath": path,
            "branch": str(conflict.get("label") or ""),
        }
    return {**base, "type": "file-conflict", "path": path, "files": [path]}


@api_view(["GET"])
def sync_conflicts(request):
    """Open conflicts in the shape the phone apps decode."""
    if not sync_daemon.is_configured():
        return Response({"conflicts": [], "unresolved_count": 0})
    try:
        conflicts = _client().conflicts()
    except sync_daemon.SyncDaemonError as exc:
        return _unavailable(exc)
    root_paths = {
        root.get("id", ""): root.get("path", "")
        for root in sync_daemon.configured_roots()
    }
    return Response(
        {
            "conflicts": [
                _legacy_conflict(conflict, root_paths)
                for conflict in conflicts
                if isinstance(conflict, dict)
            ],
            "unresolved_count": len(conflicts),
        }
    )
