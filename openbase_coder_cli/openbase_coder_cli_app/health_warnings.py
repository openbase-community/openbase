"""Aggregated health warnings for the console banner.

The console shows a top-of-page banner when something the current
configuration *expects* is not actually healthy. Expectations follow
configuration, not a fixed list: services installed by default are always
expected; conditional services (sync-daemon) are expected exactly when their
feature is configured — and conversely are flagged when running without their
feature enabled. The shared Codex service may instead be provided by Codex's
own responsive managed daemon.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

# The banner is mounted by the shared dashboard layout, so it is fetched on
# every page navigation, and each collection runs subprocess/HTTP/filesystem
# probes (service status, livekit version skew, sync health). A few seconds of
# staleness is fine for an advisory banner, so a short TTL keeps a burst of
# navigations from re-running the probes each time.
HEALTH_WARNINGS_CACHE_TTL_SECONDS = 5.0
_warnings_cache_lock = threading.Lock()
_warnings_cache: tuple[float, list[dict[str, str]]] | None = None

# Conditional services: expected exactly when the callable returns True.
# Services not listed here are expected iff install_by_default.
_CONDITIONAL_SERVICES: dict[str, Callable[[], bool]] = {}


def _sync_daemon_expected() -> bool:
    from openbase_coder_cli.sync_daemon import is_configured

    try:
        return is_configured()
    except OSError:
        return False


_CONDITIONAL_SERVICES["sync-daemon"] = _sync_daemon_expected


def _sync_daemon_warnings() -> list[dict[str, str]]:
    from openbase_coder_cli.sync_daemon import SyncDaemonClient, SyncDaemonError

    try:
        status_payload = SyncDaemonClient(timeout=0.5).status()
    except SyncDaemonError:
        return [
            _warning(
                "sync-daemon-unreachable",
                "warning",
                "Openbase Sync is configured but its daemon is not answering.",
                "Run `openbase-coder services start sync-daemon`.",
            )
        ]
    warnings: list[dict[str, str]] = []
    if not status_payload.get("peers"):
        warnings.append(
            _warning(
                "sync-daemon-no-peer",
                "warning",
                "Openbase Sync is running but not connected to the other computer.",
                "Check that the hub is on and reachable over Openbase VPN.",
            )
        )
    open_conflicts = int(status_payload.get("open_conflicts") or 0)
    if open_conflicts:
        warnings.append(
            _warning(
                "sync-daemon-conflicts",
                "warning",
                f"Openbase Sync has {open_conflicts} unresolved conflict(s).",
                "Resolve them on the Sync page.",
            )
        )
    return warnings


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
        shared_daemon = False
        if service.name == "codex-app-server" and not running:
            from openbase_coder_cli.codex_control_plane import shared_codex_daemon_ready

            shared_daemon = shared_codex_daemon_ready()
            running = shared_daemon
        if expected and not installed and not shared_daemon:
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

    Dev prefers the downloaded engine, then Homebrew/PATH. A stale download
    or fallback can differ from the release pin after a source update.
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
            "Run 'openbase-coder restart --service livekit-server' to install "
            "the pinned engine and restart voice services. Full developer "
            "setup also downloads the pinned engine.",
        )
    ]


def _codex_version_skew_warnings() -> list[dict[str, str]]:
    """Warn when a running codex-app-server predates the installed Codex.

    Long-lived services keep the old binary in memory after an upgrade, and
    every new Codex CLI launch then warns about the stale background
    service. The banner offers a one-click restart for these ids; the
    sync-workers tick also restarts them itself once nothing is in flight.
    """
    from openbase_coder_cli.services.codex_version_skew import (
        collect_codex_version_skews,
    )

    try:
        skews = collect_codex_version_skews()
    except Exception:  # noqa: BLE001 - version probe must never break health
        return []
    return [
        _warning(
            f"service-restart-needed:{skew.service}",
            "warning",
            skew.message,
            "Restart the service to pick up the installed version; it "
            "restarts automatically once no agent turn or voice call is active.",
        )
        for skew in skews
    ]


def _resolve_livekit_binary() -> str | None:
    import os

    # Mirror the service resolver's preference order: a matching pinned
    # download in ~/.openbase/bin wins, then PATH, then Homebrew.
    from openbase_coder_cli.livekit_install import (
        fallback_livekit_server_path,
        installed_livekit_server_path,
        livekit_binary_matches_pin,
    )

    pinned = installed_livekit_server_path()
    if os.access(pinned, os.X_OK) and livekit_binary_matches_pin(pinned):
        return str(pinned)
    fallback = fallback_livekit_server_path()
    return str(fallback) if fallback is not None else None


def _thread_exchange_base():
    from openbase_coder_cli.paths import OPENBASE_BASE_DIR

    return OPENBASE_BASE_DIR


def _thread_exchange_warnings() -> list[dict[str, str]]:
    """Detect silently-dead cross-device thread sync.

    Exporters happily write snapshots nobody consumes and importers find
    nothing without ever erroring. When Openbase Sync mirrors the exchange and
    a peer is *connected right now*, the exchange folder must contain a device
    directory from someone other than us (their exporter runs) and one of our
    own (ours runs). Staleness alone is deliberately not a signal: an idle
    peer with no new threads exports nothing and would false-positive.
    """
    import json as json_module

    from openbase_coder_cli.sync_daemon import (
        SyncDaemonClient,
        SyncDaemonError,
        path_is_synced,
    )

    base = _thread_exchange_base()
    if not path_is_synced(base / "thread-sync"):
        return []
    try:
        connected = bool(SyncDaemonClient(timeout=0.5).status().get("peers"))
    except SyncDaemonError:
        return []  # Daemon trouble already warned about elsewhere.
    if not connected:
        return []

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
                "snapshots from any other device — the peer's "
                "sync-workers service is probably not running.",
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
                "Check the sync-workers service here.",
            )
        )
    return warnings


def collect_warnings() -> list[dict[str, str]]:
    warnings = _service_warnings()
    warnings.extend(_installation_warnings())
    warnings.extend(_livekit_skew_warnings())
    warnings.extend(_codex_version_skew_warnings())
    if _sync_daemon_expected():
        warnings.extend(_sync_daemon_warnings())
        warnings.extend(_thread_exchange_warnings())
    return warnings


def collect_warnings_cached() -> list[dict[str, str]]:
    """``collect_warnings()`` behind a short TTL cache (see the module constant).

    The double-checked lock collapses a concurrent burst of navigations onto a
    single recomputation instead of running the probes once per request.
    """
    global _warnings_cache

    cached = _warnings_cache
    if (
        cached is not None
        and time.monotonic() - cached[0] < HEALTH_WARNINGS_CACHE_TTL_SECONDS
    ):
        return cached[1]

    with _warnings_cache_lock:
        cached = _warnings_cache
        if (
            cached is not None
            and time.monotonic() - cached[0] < HEALTH_WARNINGS_CACHE_TTL_SECONDS
        ):
            return cached[1]
        warnings = collect_warnings()
        _warnings_cache = (time.monotonic(), warnings)
        return warnings


@api_view(["GET", "POST"])
def health_warnings(request):
    """Warnings the console surfaces in its top banner."""
    from openbase_coder_cli.services.freshness.collector import collect_freshness

    payload = {"warnings": collect_warnings_cached()}
    # An opt-in read-only POST carries the loaded renderer/main build stamps.
    # Existing GET consumers and production clients do no source scanning.
    if request.method == "POST":
        client = request.data if isinstance(request.data, dict) else None
        payload["freshness"] = collect_freshness(client)
    return Response(payload, status=status.HTTP_200_OK)
