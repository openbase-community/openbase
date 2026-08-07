"""Code-sync settings/status/conflicts API views for the console and apps."""

from __future__ import annotations

import logging

import httpx
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import sync_config
from openbase_coder_cli.code_sync import CodeSyncError
from openbase_coder_cli.code_sync import manager as sync_manager
from openbase_coder_cli.code_sync.conflicts import (
    BRANCH_CONFLICT_KIND,
    FILE_CONFLICT_KIND,
    conflict_device_hint,
    containing_folder_for_conflict_path,
    find_conflict,
    ignore_pattern_for_containing_folder,
    mark_file_conflicts_resolved_under,
    original_relpath_for_conflict_path,
    resolve_conflict,
    unresolved_conflicts,
)
from openbase_coder_cli.code_sync.eligibility import (
    current_eligibility,
    deregister_cloud_device,
)
from openbase_coder_cli.code_sync.lease import local_activity_recent
from openbase_coder_cli.code_sync.reconciler import read_reconcile_state
from openbase_coder_cli.code_sync.syncthing import (
    SyncthingClient,
    stored_device_id,
)
from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
)

logger = logging.getLogger(__name__)


class SyncFolderSerializer(serializers.Serializer):
    relpath = serializers.CharField()
    extra_ignores = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )


class SyncSettingsSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False)
    folders = SyncFolderSerializer(many=True, required=False)
    lease_mode = serializers.ChoiceField(
        choices=sync_config.LEASE_MODES, required=False
    )


def _settings_payload(**extra) -> dict:
    eligibility = current_eligibility()
    return {
        "schema_version": sync_config.SYNC_CONFIG_SCHEMA_VERSION,
        "enabled": sync_config.code_sync_enabled(),
        "eligible": eligibility.eligible,
        "eligible_reason": eligibility.reason,
        "self_device_id": stored_device_id() or "",
        "peers": [peer.to_dict() for peer in eligibility.peers],
        "folders": [folder.to_dict() for folder in sync_config.sync_folders()],
        "lease_mode": sync_config.lease_mode(),
        "versions_usage_bytes": sync_manager.versions_usage_bytes(),
        **extra,
    }


def _sync_conflict_payload(conflict: dict) -> dict:
    folder = sync_config.folder_for_id(str(conflict.get("folder_id") or ""))
    folder_relpath = folder.relpath if folder is not None else ""
    base = {
        "id": conflict.get("id", ""),
        "kind": conflict.get("kind", ""),
        "folder_id": conflict.get("folder_id", ""),
        "folder_relpath": folder_relpath,
        "detected_at": conflict.get("detected_at", ""),
        "resolved": bool(conflict.get("resolved")),
    }
    if conflict.get("kind") == BRANCH_CONFLICT_KIND:
        return {
            **base,
            "type": "repo-divergence",
            "repo_relpath": conflict.get("repo_relpath", ""),
            "branch": conflict.get("branch", ""),
            "local_sha": conflict.get("local_sha", ""),
            "remote_sha": conflict.get("remote_sha", ""),
        }
    if conflict.get("kind") != FILE_CONFLICT_KIND:
        return {**base, "type": "unknown"}

    path = str(conflict.get("path") or "")
    containing_folder = None
    original_path = ""
    device_hint = ""
    try:
        containing_folder = containing_folder_for_conflict_path(path)
        original_path = original_relpath_for_conflict_path(path)
        device_hint = conflict_device_hint(path)
    except CodeSyncError:
        pass

    conflict_copy_exists = False
    original_exists = False
    ignored_containing_folder = False
    if folder is not None:
        folder_root = folder.absolute_path()
        conflict_copy_exists = bool(path and (folder_root / path).is_file())
        original_exists = bool(original_path and (folder_root / original_path).exists())
        ignored_containing_folder = bool(
            containing_folder and f"/{containing_folder}" in folder.extra_ignores
        )

    return {
        **base,
        "type": "file-conflict",
        "path": path,
        "files": [path] if path else [],
        "containing_folder": containing_folder,
        "original_path": original_path,
        "conflict_device_hint": device_hint,
        "conflict_copy_exists": conflict_copy_exists,
        "original_exists": original_exists,
        "ignored_containing_folder": ignored_containing_folder,
    }


@api_view(["GET", "PUT"])
def sync_settings(request):
    """Read or update code-sync settings (folders use full-list replace)."""
    if request.method == "GET":
        return Response(_settings_payload(), status=status.HTTP_200_OK)

    serializer = SyncSettingsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    if not data:
        return Response(
            {"error": "Provide enabled, folders, or lease_mode."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        if "folders" in data:
            sync_config.set_sync_folders(data["folders"])
        if "lease_mode" in data:
            sync_config.set_lease_mode(data["lease_mode"])
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    warning = ""
    try:
        if "enabled" in data:
            if data["enabled"] and not sync_config.code_sync_enabled():
                sync_manager.enable_code_sync(force=True)
            elif not data["enabled"] and sync_config.code_sync_enabled():
                sync_manager.disable_code_sync()
            elif data["enabled"]:
                sync_manager.apply_settings_change()
        elif "folders" in data or "lease_mode" in data:
            sync_manager.apply_settings_change()
    except CodeSyncError as exc:
        logger.warning("code_sync apply_settings_failed: %s", exc)
        warning = str(exc)

    return Response(
        _settings_payload(changed=True, apply_warning=warning),
        status=status.HTTP_200_OK,
    )


class SyncPeerRemoveSerializer(serializers.Serializer):
    device_id = serializers.CharField()


@api_view(["POST"])
def sync_peers_remove(request):
    """Forget a sync peer by deregistering its device from the cloud registry.

    This is how a user dismisses a machine they have shut down: the device is
    removed from the account registry (single source of truth for peers), then
    a settings reconcile drops it from the Syncthing configuration so the
    "peer disconnected" health warning clears.
    """
    serializer = SyncPeerRemoveSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    device_id = serializer.validated_data["device_id"].strip()
    if not device_id:
        return Response(
            {"error": "Provide the device_id of the peer to remove."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        deregister_cloud_device(device_id)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except AuthLoginRequiredError as exc:
        return Response(
            {"error": f"Login required to remove sync peer: {exc}"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    except (AuthTransientError, httpx.HTTPError) as exc:
        logger.warning("sync_peers_remove deregister_failed: %s", exc)
        return Response(
            {"error": f"Unable to remove sync peer: {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    warning = ""
    if sync_config.code_sync_enabled():
        try:
            sync_manager.apply_settings_change()
        except CodeSyncError as exc:
            logger.warning("sync_peers_remove apply_settings_failed: %s", exc)
            warning = str(exc)

    return Response(
        _settings_payload(changed=True, apply_warning=warning),
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
def sync_status(request):
    """Live sync health per folder plus reconcile/lease facts."""
    enabled = sync_config.code_sync_enabled()
    folders = []
    client = None
    if enabled:
        try:
            client = SyncthingClient()
        except CodeSyncError:
            client = None
    for folder in sync_config.sync_folders():
        entry = {
            "id": folder.folder_id,
            "relpath": folder.relpath,
            "state": "unknown",
            "completion": None,
            "receive_only": False,
            "peer_completion": {},
        }
        if client is not None:
            try:
                folder_status = client.folder_status(folder.folder_id)
                entry["state"] = str(folder_status.get("state") or "unknown")
                completion = client.folder_completion(folder.folder_id)
                entry["completion"] = completion.get("completion")
                folder_config = client.folder_config(folder.folder_id)
                entry["receive_only"] = folder_config.get("type") == "receiveonly"
            except CodeSyncError as exc:
                entry["state"] = "unreachable"
                entry["error"] = str(exc)
        folders.append(entry)

    reconcile_state = read_reconcile_state()
    return Response(
        {
            "enabled": enabled,
            "active": local_activity_recent(),
            "lease_mode": sync_config.lease_mode(),
            "lease_holder_device_id": sync_config.lease_holder_device_id() or "",
            "folders": folders,
            "last_reconcile_at": reconcile_state.get("last_reconcile_at", ""),
            "conflicts_count": len(unresolved_conflicts()),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
def sync_conflicts(request):
    """Unresolved sync conflict records (branch divergences and file conflicts)."""
    conflicts = unresolved_conflicts()
    return Response(
        {
            "conflicts": [_sync_conflict_payload(conflict) for conflict in conflicts],
            "unresolved_count": len(conflicts),
        },
        status=status.HTTP_200_OK,
    )


class SyncConflictResolveSerializer(serializers.Serializer):
    id = serializers.CharField()
    action = serializers.ChoiceField(choices=("keep_local", "use_remote"))


class SyncConflictIgnoreFolderSerializer(serializers.Serializer):
    id = serializers.CharField()


@api_view(["POST"])
def sync_conflicts_resolve(request):
    """Resolve one conflict with keep_local or use_remote."""
    serializer = SyncConflictResolveSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        record = resolve_conflict(
            serializer.validated_data["id"], serializer.validated_data["action"]
        )
    except CodeSyncError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"conflict": record}, status=status.HTTP_200_OK)


@api_view(["POST"])
def sync_conflicts_ignore_containing_folder(request):
    """Ignore a file conflict's containing folder and clear matching records."""
    serializer = SyncConflictIgnoreFolderSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    conflict_id = serializer.validated_data["id"]
    conflict = find_conflict(conflict_id)
    if conflict is None:
        return Response(
            {"error": f"No conflict with id {conflict_id!r}."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        ignore_pattern = ignore_pattern_for_containing_folder(conflict)
    except CodeSyncError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    folder = sync_config.folder_for_id(str(conflict.get("folder_id") or ""))
    if folder is None:
        return Response(
            {
                "error": (
                    "Conflict references unknown sync folder "
                    f"{conflict.get('folder_id')!r}."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    folders = list(sync_config.sync_folders())
    updated_folders = []
    added = False
    for entry in folders:
        extra_ignores = list(entry.extra_ignores)
        if entry.folder_id == folder.folder_id and ignore_pattern not in extra_ignores:
            extra_ignores.append(ignore_pattern)
            added = True
        updated_folders.append(
            {"relpath": entry.relpath, "extra_ignores": extra_ignores}
        )

    try:
        sync_config.set_sync_folders(updated_folders)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    warning = ""
    try:
        sync_manager.apply_settings_change()
    except CodeSyncError as exc:
        logger.warning("code_sync ignore_conflict_folder_apply_failed: %s", exc)
        warning = str(exc)

    folder_relpath = ignore_pattern.lstrip("/")
    resolved_count = mark_file_conflicts_resolved_under(
        folder_id=folder.folder_id,
        folder_relpath=folder_relpath,
        resolution="ignored_containing_folder",
    )
    return Response(
        {
            "ignore_pattern": ignore_pattern,
            "added": added,
            "resolved_count": resolved_count,
            "apply_warning": warning,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
def sync_versions_purge(request):
    """Delete all Syncthing version copies and report freed bytes."""
    freed = sync_manager.purge_versions()
    return Response({"freed_bytes": freed}, status=status.HTTP_200_OK)
