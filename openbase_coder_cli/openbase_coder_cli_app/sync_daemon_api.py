"""Openbase Sync daemon API: thin proxies to the daemon's control socket.

``/api/sync/daemon/*`` serves the console Sync page. ``/api/sync/status/``,
``/api/sync/conflicts/`` and ``/api/sync/conflicts/resolve/`` keep the
response shapes the phone apps already decode, now backed by the daemon.
The daemon owns all state; nothing here stores any.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import sync_daemon, sync_state


def _client(timeout: float | None = None) -> sync_daemon.SyncDaemonClient:
    if timeout is None:
        return sync_daemon.SyncDaemonClient()
    return sync_daemon.SyncDaemonClient(timeout=timeout)


def _unavailable(exc: Exception) -> Response:
    return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


@api_view(["GET"])
def sync_daemon_settings(request):
    """Configuration summary, whether the daemon answers, and the hub.

    Never includes the pair secret.
    """
    from openbase_coder_cli import sync_pairing

    summary = sync_daemon.read_config_summary()
    summary["reachable"] = summary["configured"] and sync_daemon.reachable()
    summary.update(sync_pairing.hub_display(summary))
    return Response(summary)


def _stale_lock_count(snapshot: dict[str, Any]) -> int | None:
    locks = snapshot.get("locks")
    if locks is None:
        return None
    return sum(len(paths) for paths in locks.values())


@api_view(["GET"])
def sync_daemon_status(request):
    """The daemon's status plus ``overview``: health, backlog, attention."""
    try:
        payload = _client().status()
        payload["metrics"] = _client().metrics()
    except sync_daemon.SyncDaemonError as exc:
        return _unavailable(exc)
    peers = [peer for peer in payload.get("peers") or [] if isinstance(peer, dict)]
    sync_state.PRESENCE.observe(peers, time.time())
    payload["overview"] = sync_state.overview(
        payload,
        config_roots=sync_daemon.configured_roots(),
        offline_peers=sync_state.PRESENCE.offline(
            str(peer.get("device") or "") for peer in peers
        ),
        stale_lock_count=_stale_lock_count(sync_state.STALE_LOCKS.snapshot()),
    )
    return Response(payload)


def _local_device() -> str:
    return str(sync_daemon.read_config_summary().get("device_id") or "")


@api_view(["GET"])
def sync_daemon_conflicts(request):
    """Open conflicts, with the repository or folder each belongs to."""
    try:
        conflicts = _client().conflicts(request.query_params.get("root") or None)
    except sync_daemon.SyncDaemonError as exc:
        return _unavailable(exc)
    enriched = sync_state.enrich_conflicts(
        conflicts,
        roots=sync_daemon.configured_roots(),
        local_device=_local_device(),
    )
    return Response({"conflicts": enriched, "unresolved_count": len(enriched)})


@api_view(["GET"])
def sync_daemon_conflict_detail(request, conflict_id: int):
    """Both sides of one conflict.

    Text conflicts: each version's size and, for small UTF-8 text, its
    content, read from the daemon's version store, plus a diff. Branch
    conflicts: where each computer's branch points and the commits only one
    side has.
    """
    try:
        conflicts = _client().conflicts()
    except sync_daemon.SyncDaemonError as exc:
        return _unavailable(exc)
    conflict = sync_state.find_conflict(conflicts, int(conflict_id))
    if conflict is None:
        return Response(
            {"error": "This conflict is no longer open."},
            status=status.HTTP_404_NOT_FOUND,
        )
    roots = sync_daemon.configured_roots()
    root_path = next(
        (
            Path(str(root["path"])).expanduser()
            for root in roots
            if root.get("id") == conflict.get("root") and root.get("path")
        ),
        None,
    )
    local_device = _local_device()
    if conflict.get("kind") == "git-branch":
        detail = sync_state.git_branch_detail(
            conflict, root_path=root_path, local_device=local_device
        )
    else:
        detail = sync_state.file_conflict_detail(
            conflict,
            store=sync_daemon.state_dir() / "versions",
            root_path=root_path,
        )
    enriched = sync_state.enrich_conflicts(
        [conflict], roots=roots, local_device=local_device
    )[0]
    return Response({"conflict": enriched, "detail": detail})


_RESOLVE_CHOICES = {
    "keep_local": "a",
    "keep_mine": "a",
    "a": "a",
    "use_remote": "b",
    "take_theirs": "b",
    "b": "b",
}

MAX_BULK_RESOLVE = 500

GIT_BRANCH_REFUSAL = sync_state.GIT_BRANCH_REFUSAL


def _resolve_one(
    client: sync_daemon.SyncDaemonClient,
    open_by_id: dict[int, dict[str, Any]],
    conflict_id: int,
    action: str,
    local_device: str,
) -> tuple[int, str | None]:
    """(HTTP status, error) for one resolution."""
    conflict = open_by_id.get(conflict_id)
    if conflict is None:
        return status.HTTP_404_NOT_FOUND, "This conflict is no longer open."
    if conflict.get("kind") == "git-branch":
        return status.HTTP_409_CONFLICT, GIT_BRANCH_REFUSAL
    choice = sync_state.resolution_choice(conflict, action, local_device)
    try:
        client.resolve(conflict_id, choice)
    except sync_daemon.SyncDaemonError as exc:
        return status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)
    return status.HTTP_200_OK, None


@api_view(["POST"])
def sync_daemon_conflicts_resolve(request):
    """Resolve one conflict (``id``) or several (``ids``) the same way.

    ``action`` is ``keep_local`` (this computer's version wins) or
    ``use_remote`` (take the other computer's). Branch conflicts are refused:
    the daemon does not move refs, so neither action would do what it says.
    """
    data = request.data if isinstance(request.data, dict) else {}
    action = str(data.get("action") or "")
    choice = _RESOLVE_CHOICES.get(action)
    raw_ids = data.get("ids")
    bulk = isinstance(raw_ids, list)
    if bulk:
        ids_in = raw_ids
    elif data.get("id") is not None:
        ids_in = [data.get("id")]
    else:
        ids_in = []
    if not ids_in or choice is None:
        return Response(
            {"error": "id (or ids) and action (keep_local|use_remote) are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if len(ids_in) > MAX_BULK_RESOLVE:
        return Response(
            {"error": f"Resolve at most {MAX_BULK_RESOLVE} conflicts per request."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    ids: list[int] = []
    for raw in ids_in:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            return Response(
                {"error": f"Unknown conflict id {raw!r}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    client = _client(timeout=sync_state.RESOLVE_TIMEOUT_S)
    try:
        open_by_id = {
            int(conflict.get("id")): conflict
            for conflict in client.conflicts()
            if isinstance(conflict, dict) and conflict.get("id") is not None
        }
    except sync_daemon.SyncDaemonError as exc:
        return _unavailable(exc)
    local_device = _local_device()
    if not bulk:
        code, error = _resolve_one(client, open_by_id, ids[0], action, local_device)
        if error:
            return Response({"error": error, "id": ids[0]}, status=code)
        return Response(
            {
                "resolved": True,
                "id": ids[0],
                "choice": sync_state.resolution_choice(
                    open_by_id[ids[0]], action, local_device
                ),
            }
        )
    results = []
    for conflict_id in ids:
        code, error = _resolve_one(
            client, open_by_id, conflict_id, action, local_device
        )
        results.append({"id": conflict_id, "ok": error is None, "error": error})
        if code == status.HTTP_503_SERVICE_UNAVAILABLE:
            # the daemon stopped answering: do not hammer it with the rest
            results += [
                {"id": rest, "ok": False, "error": "not attempted"}
                for rest in ids[len(results) :]
            ]
            break
    resolved = sum(1 for result in results if result["ok"])
    return Response(
        {
            "resolved": resolved,
            "failed": len(results) - resolved,
            "choice": choice,
            "results": results,
        }
    )


@api_view(["GET"])
def sync_daemon_stale_locks(request):
    """Git lock files left by a process that died, from the last daemon scan.

    The daemon needs minutes to scan a large tree, so this answers from the
    last scan and refreshes in the background (``?refresh=1`` forces one).
    """
    if not sync_daemon.is_configured():
        return Response(
            {
                "locks": [],
                "checked_at": None,
                "refreshing": False,
                "error": None,
                "stale_after_s": sync_state.STALE_LOCK_AGE_S,
            }
        )
    refresh = str(request.query_params.get("refresh") or "") in {"1", "true"}
    snapshot = sync_state.STALE_LOCKS.snapshot(refresh=refresh)
    return Response(
        {
            "locks": sync_state.describe_stale_locks(snapshot["locks"]),
            "checked_at": snapshot["checked_at"],
            "refreshing": snapshot["refreshing"],
            "error": snapshot["error"],
            "stale_after_s": sync_state.STALE_LOCK_AGE_S,
        }
    )


@api_view(["POST"])
def sync_daemon_stale_lock_trash(request):
    """Move one stale git lock into ``~/.openbase/trash`` (never deletes)."""
    data = request.data if isinstance(request.data, dict) else {}
    path = str(data.get("path") or "")
    try:
        result = sync_state.move_lock_to_trash(path, known=sync_state.STALE_LOCKS.known)
    except sync_state.LockMoveError as exc:
        return Response({"error": str(exc)}, status=exc.status)
    except OSError as exc:
        return Response(
            {"error": f"Could not move the lock: {exc}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    sync_state.STALE_LOCKS.forget(path)
    return Response(result)


@api_view(["GET", "POST"])
def sync_daemon_held_deletes(request):
    """Deletions held by the mass-delete guard; POST releases or discards one
    synced folder's (``root``), or only its hold on ``folder`` (root-relative)."""
    if not sync_daemon.is_configured():
        return Response({"roots": []})
    try:
        client = _client(timeout=30)
        if request.method == "POST":
            data = request.data if isinstance(request.data, dict) else {}
            root_id = str(data.get("root") or "")
            action = str(data.get("action") or "")
            folder = str(data.get("folder") or "").strip("/") or None
            if not root_id or action not in {"release", "discard"}:
                return Response(
                    {"error": "Pass a folder id and action 'release' or 'discard'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            done = (
                client.release_deletes(root_id, folder)
                if action == "release"
                else client.discard_deletes(root_id, folder)
            )
            result = {"root": root_id, "action": action, "count": done}
            if folder:
                result["folder"] = folder
            return Response(result)
        return Response(
            {"roots": sync_state.held_deletes_summary(client, client.status())}
        )
    except sync_daemon.SyncDaemonError as exc:
        return _unavailable(exc)


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


def _legacy_conflict(
    conflict: dict[str, Any], root_paths: dict[str, str], local_device: str
) -> dict:
    kind = str(conflict.get("kind") or "")
    path = str(conflict.get("path") or "")
    root_id = str(conflict.get("root") or "")
    a_is_local = not local_device or str(conflict.get("a_device") or "") == local_device
    remote_device = conflict.get("b_device" if a_is_local else "a_device")
    base = {
        "id": str(conflict.get("id")),
        "kind": kind,
        "folder_id": root_id,
        "folder_relpath": _display_relpath(root_paths.get(root_id, "")),
        "detected_at": _iso_from_ns(conflict.get("created_ns")),
        "resolved": False,
        "conflict_device_hint": str(remote_device or ""),
        "label": str(conflict.get("label") or ""),
    }
    if kind == "git-branch":
        repo, _, ref = path.partition(":")
        return {
            **base,
            "type": "repo-divergence",
            "repo_relpath": repo or path,
            "branch": ref.removeprefix("refs/heads/") if ref else "",
            "local_sha": str(conflict.get("a_hash" if a_is_local else "b_hash") or ""),
            "remote_sha": str(conflict.get("b_hash" if a_is_local else "a_hash") or ""),
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
    local_device = _local_device()
    return Response(
        {
            "conflicts": [
                _legacy_conflict(conflict, root_paths, local_device)
                for conflict in conflicts
                if isinstance(conflict, dict)
            ],
            "unresolved_count": len(conflicts),
        }
    )
