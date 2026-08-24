"""Aggregated health warnings for the console banner.

The console shows a top-of-page banner when something the current
configuration *expects* is not actually healthy. Expectations follow
configuration, not a fixed list: services installed by default are always
expected; conditional services (code-sync) are expected exactly when their
feature is enabled — and conversely are flagged when running without their
feature enabled.
"""

from __future__ import annotations

import calendar
import shutil
import time
from typing import Callable

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

RECONCILE_STALE_SECONDS = 10 * 60
# Warn while there is still room to act; the engine's hard pause floor is
# MIN_DISK_FREE_MB (2 GiB) in the rendered Syncthing config.
SYNC_DISK_LOW_BYTES = 5 * 1024 * 1024 * 1024

# Conditional services: expected exactly when the callable returns True.
# Services not listed here are expected iff install_by_default.
_CONDITIONAL_SERVICES: dict[str, Callable[[], bool]] = {}


def _code_sync_expected() -> bool:
    from openbase_coder_cli.sync_config import code_sync_enabled

    try:
        return code_sync_enabled()
    except ValueError:
        return False


_CONDITIONAL_SERVICES["code-sync"] = _code_sync_expected
# Cross-device thread sync rides the code-sync transport; when devices are
# mirrored, both backends' device-sync services are expected too.
_CONDITIONAL_SERVICES["codex-thread-device-sync"] = _code_sync_expected
_CONDITIONAL_SERVICES["claude-thread-device-sync"] = _code_sync_expected


def _warning(
    warning_id: str, severity: str, message: str, action: str = ""
) -> dict[str, str]:
    return {
        "id": warning_id,
        "severity": severity,  # "warning" | "critical"
        "message": message,
        "action": action,
    }


def _service_warnings() -> list[dict[str, str]]:
    from openbase_coder_cli.services.definitions import SERVICES
    from openbase_coder_cli.services.launchd import launchctl_status
    from openbase_coder_cli.services.selection import (
        service_supports_configured_backends,
    )

    warnings: list[dict[str, str]] = []
    for service in SERVICES:
        if not service_supports_configured_backends(service):
            # Backend-scoped services are intentionally absent when another
            # coding backend is selected.
            continue
        conditional = _CONDITIONAL_SERVICES.get(service.name)
        expected = conditional() if conditional else service.install_by_default
        try:
            info = launchctl_status(service)
        except Exception:  # noqa: BLE001 - status probe must never break health
            continue
        installed = bool(info.get("installed"))
        running = bool(info.get("pid"))
        if expected and not installed:
            warnings.append(
                _warning(
                    f"service-missing:{service.name}",
                    "critical",
                    f"Expected service '{service.name}' is not installed.",
                    "Run 'openbase-coder services install'.",
                )
            )
        elif expected and not running:
            warnings.append(
                _warning(
                    f"service-stopped:{service.name}",
                    "critical",
                    f"Expected service '{service.name}' is not running "
                    f"(last exit: {info.get('last_exit_code', 'unknown')}).",
                    "Run 'openbase-coder restart'.",
                )
            )
        elif not expected and conditional is not None and installed:
            warnings.append(
                _warning(
                    f"service-unexpected:{service.name}",
                    "warning",
                    f"Service '{service.name}' is installed but its feature "
                    "is disabled.",
                    "Disable removed the feature; uninstall the service or "
                    "re-enable the feature.",
                )
            )
    return warnings


def _sync_warnings() -> list[dict[str, str]]:
    from openbase_coder_cli.code_sync import CodeSyncError
    from openbase_coder_cli.code_sync import manager as sync_manager
    from openbase_coder_cli.code_sync.ignores import STIGNORE_FILENAME
    from openbase_coder_cli.code_sync.reconciler import read_reconcile_state
    from openbase_coder_cli.code_sync.syncthing import SyncthingClient
    from openbase_coder_cli.paths import CODE_SYNC_DIR
    from openbase_coder_cli.services.tailnet_devices import tailscale_self_identity
    from openbase_coder_cli.sync_config import sync_folders

    warnings: list[dict[str, str]] = []

    # Engine reachable + peers connected + folders actually syncing.
    try:
        client = SyncthingClient()
        for device_id, conn in client.connections().items():
            if not conn.get("connected") and not conn.get("paused"):
                warnings.append(
                    _warning(
                        f"sync-peer-disconnected:{device_id[:7]}",
                        "critical",
                        f"Sync peer {device_id[:7]}… is not connected; file "
                        "sync between your machines is stopped.",
                        "Check the peer machine is on and on the tailnet; "
                        "see the file-sync skill for half-open connections.",
                    )
                )
        # A folder in error state (e.g. "insufficient space on disk") stops
        # syncing while the engine stays up — historically invisible. One
        # warning per distinct error message, not per folder.
        seen_folder_errors: dict[str, str] = {}
        for folder in sync_folders():
            folder_status = client.folder_status(folder.folder_id)
            error = str(folder_status.get("error") or "")
            if error and error not in seen_folder_errors:
                seen_folder_errors[error] = folder.relpath
        for error, relpath in seen_folder_errors.items():
            warnings.append(
                _warning(
                    f"sync-folder-error:{relpath}",
                    "critical",
                    f"Sync folder '{relpath}' has stopped syncing: {error[:200]}",
                    "Free disk space on this Mac, then restart Openbase "
                    "services; see the code-sync docs troubleshooting.",
                )
            )
    except CodeSyncError as exc:
        warnings.append(
            _warning(
                "sync-engine-unreachable",
                "critical",
                f"Code sync is enabled but its engine is unreachable: {exc}",
                "Run 'openbase-coder restart' or 'openbase-coder sync enable'.",
            )
        )

    # Low disk pauses Syncthing folders (absolute floor MIN_DISK_FREE_MB in
    # the rendered config); warn before that line is crossed.
    try:
        free_bytes = shutil.disk_usage(str(CODE_SYNC_DIR)).free
    except OSError:
        free_bytes = None
    if free_bytes is not None and free_bytes < SYNC_DISK_LOW_BYTES:
        warnings.append(
            _warning(
                "sync-disk-low",
                "warning",
                f"Only {free_bytes // (1024 * 1024 * 1024)} GiB of disk is "
                "free; below 2 GiB file sync pauses entirely.",
                "Free disk space on this Mac.",
            )
        )

    # This device must advertise a tailscale identity or peers will drop it.
    identity = tailscale_self_identity()
    if not identity.get("available"):
        warnings.append(
            _warning(
                "sync-no-tailscale-identity",
                "critical",
                "This device's registration has no Tailscale identity; "
                "peers will drop it from their sync configuration.",
                identity.get("error") or "Check Tailscale is installed and up.",
            )
        )

    # A second, user-managed Syncthing syncing the same folders echoes
    # writes into conflict storms.
    try:
        sync_manager.ensure_no_user_managed_overlap(sync_folders())
    except CodeSyncError as exc:
        warnings.append(_warning("sync-user-managed-overlap", "critical", str(exc), ""))

    # Reconciler heartbeat: git branch pointers stop propagating silently.
    state = read_reconcile_state()
    last = state.get("last_reconcile_at")
    if last:
        try:
            # The timestamp is UTC; calendar.timegm avoids the DST-unaware
            # time.timezone arithmetic that added a phantom hour.
            last_epoch = calendar.timegm(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ"))
            stale = time.time() - last_epoch
        except ValueError:
            stale = None
        if stale is not None and stale > RECONCILE_STALE_SECONDS:
            warnings.append(
                _warning(
                    "sync-reconcile-stale",
                    "warning",
                    f"Git-state reconciliation last ran {int(stale // 60)} "
                    "minutes ago; commits are not propagating.",
                    "Check the sync-workers service.",
                )
            )

    # Managed ignore integrity: losing the VCS block silently syncs .git.
    for folder in sync_folders():
        stignore = folder.absolute_path() / STIGNORE_FILENAME
        try:
            content = stignore.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        if "(?d).git" not in content:
            warnings.append(
                _warning(
                    f"sync-stignore-broken:{folder.folder_id}",
                    "critical",
                    f"Sync folder '{folder.relpath}' is missing its .git "
                    "ignore; syncing .git corrupts repositories.",
                    "Run 'openbase-coder sync enable' to regenerate it.",
                )
            )
    return warnings


def _installation_warnings() -> list[dict[str, str]]:
    """Warn when a dev workspace exists but a packaged install serves it.

    The two sanctioned installs (see the workspace glossary's Installation
    pathways) are mutually exclusive on one machine in practice: if the
    Projects list tracks an ``openbase-coder-workspace`` checkout, the
    developer expects localhost:7999 to serve that code — a standalone
    (app-installed) runtime silently serves something older instead.
    """
    from openbase_coder_cli.services.installation import InstallationConfig
    from openbase_coder_cli.thread_sync.projects import get_recent_projects

    try:
        if not InstallationConfig.exists():
            return []
        config = InstallationConfig.load()
    except Exception:  # noqa: BLE001 - health must never break on bad state
        return []
    if not config.standalone:
        return []

    for project in get_recent_projects():
        path = project.get("path", "")
        if path.rstrip("/").split("/")[-1] == "openbase-coder-workspace":
            return [
                _warning(
                    "installation-not-dev",
                    "warning",
                    "An openbase-coder-workspace checkout is in your "
                    "projects, but this machine runs a packaged install — "
                    "the code on disk is not what localhost:7999 serves.",
                    "For development, archive ~/.openbase and run the "
                    "workspace's ./scripts/setup (dev pathway).",
                )
            ]
    return []


def _livekit_skew_warnings() -> list[dict[str, str]]:
    """Warn dev installs whose livekit-server differs from the release pin.

    Dev resolves livekit-server from Homebrew/PATH while releases bundle
    the pinned version; a divergence means development tests a different
    voice engine than users run.
    """
    import re
    import subprocess

    from openbase_coder_cli.livekit_version import LIVEKIT_SERVER_PINNED_VERSION
    from openbase_coder_cli.services.installation import InstallationConfig

    try:
        if not InstallationConfig.exists() or InstallationConfig.load().standalone:
            return []  # Standalone installs run the bundled pin by construction.
    except Exception:  # noqa: BLE001
        return []

    binary = _resolve_livekit_binary()
    if binary is None:
        return []  # Missing binary surfaces through service checks instead.
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    match = re.search(r"(\d+\.\d+\.\d+)", result.stdout + result.stderr)
    if not match or match.group(1) == LIVEKIT_SERVER_PINNED_VERSION:
        return []
    return [
        _warning(
            "livekit-version-skew",
            "warning",
            f"This dev install runs livekit-server {match.group(1)}, but "
            f"releases ship {LIVEKIT_SERVER_PINNED_VERSION} — voice testing "
            "here exercises a different engine than users run.",
            "Run 'openbase-coder setup' to download the pinned engine into "
            "~/.openbase/bin, or bump the pin in livekit_version.py "
            "deliberately.",
        )
    ]


def _resolve_livekit_binary() -> str | None:
    import os
    import shutil

    from openbase_coder_cli.paths import OPENBASE_BIN_DIR

    # Mirror the service resolver's preference order: the pinned download in
    # ~/.openbase/bin wins, then PATH, then Homebrew.
    pinned = OPENBASE_BIN_DIR / "livekit-server"
    if os.access(pinned, os.X_OK):
        return str(pinned)
    found = shutil.which("livekit-server")
    if found:
        return found
    fallback = "/opt/homebrew/bin/livekit-server"
    return fallback if os.access(fallback, os.X_OK) else None


def _thread_exchange_base():
    from openbase_coder_cli.paths import OPENBASE_BASE_DIR

    return OPENBASE_BASE_DIR


def _thread_exchange_warnings() -> list[dict[str, str]]:
    """Detect silently-dead cross-device thread sync.

    Exporters happily write snapshots nobody consumes and importers find
    nothing without ever erroring. When a sync peer is *connected right
    now*, the exchange folder must contain a device directory from someone
    other than us (their exporter runs) and one of our own (ours runs).
    Staleness alone is deliberately not a signal: an idle peer with no new
    threads exports nothing and would false-positive.
    """
    import json as json_module

    from openbase_coder_cli.code_sync import CodeSyncError
    from openbase_coder_cli.code_sync import syncthing as syncthing_module

    try:
        client = syncthing_module.SyncthingClient()
        connected = any(conn.get("connected") for conn in client.connections().values())
    except CodeSyncError:
        return []  # Engine trouble already warned about elsewhere.
    if not connected:
        return []

    base = _thread_exchange_base()
    exchange = base / "thread-sync" / "devices"
    own_id = ""
    try:
        own_id = json_module.loads((base / "thread-sync-device.json").read_text()).get(
            "device_id", ""
        )
    except (OSError, ValueError):
        pass

    try:
        device_dirs = [p.name for p in exchange.iterdir() if p.is_dir()]
    except OSError:
        device_dirs = []

    warnings: list[dict[str, str]] = []
    if own_id and not any(name != own_id for name in device_dirs):
        warnings.append(
            _warning(
                "thread-sync-no-peer-snapshots",
                "warning",
                "A sync peer is connected but the thread exchange has no "
                "snapshots from any other device — the peer's thread "
                "device-sync services are probably not running.",
                "Check 'openbase-coder services status' on the peer.",
            )
        )
    if own_id and own_id not in device_dirs:
        warnings.append(
            _warning(
                "thread-sync-not-exporting",
                "warning",
                "A sync peer is connected but this device has never "
                "exported a thread snapshot.",
                "Check the codex/claude-thread-device-sync services here.",
            )
        )
    return warnings


def collect_warnings() -> list[dict[str, str]]:
    warnings = _service_warnings()
    warnings.extend(_installation_warnings())
    warnings.extend(_livekit_skew_warnings())
    if _code_sync_expected():
        warnings.extend(_sync_warnings())
        warnings.extend(_thread_exchange_warnings())
    return warnings


@api_view(["GET"])
def health_warnings(request):
    """Warnings the console surfaces in its top banner."""
    return Response({"warnings": collect_warnings()}, status=status.HTTP_200_OK)
