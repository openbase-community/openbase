"""Developer source freshness, separate from service health and release updates."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import click
import psutil

from openbase_coder_cli.services.freshness import runtime
from openbase_coder_cli.services.freshness.compare import compare_build, detail
from openbase_coder_cli.services.freshness.source import (
    file_digest,
    read_manifest,
    revision,
    workspace_id,
)

_CACHE_SECONDS = 10
_cache: tuple[float, str, dict] | None = None
_lock = threading.Lock()


def _service_detail(service, pid: int, workspace: Path, current: dict) -> dict:
    action = f"Restart {service.name}."
    unknown = detail(
        service.description,
        "unknown",
        "Running process has no verified startup stamp.",
        action,
    )
    record = read_manifest(runtime.RECORD_DIR / f"{service.name}.json")
    if not record or record.get("pid") != pid:
        return unknown
    identity = runtime.process_identity(pid)
    if identity is None or identity != record.get("process_start"):
        return unknown
    if record.get("workspace_id") != workspace_id(workspace):
        return detail(
            service.description,
            "stale",
            "Running from a different source workspace.",
            action,
        )
    if record.get("kind") != service.freshness_kind:
        return unknown
    if service.freshness_kind == "build":
        return compare_build(record.get("artifact"), service.name, workspace, current)
    if service.freshness_kind == "binary":
        from openbase_coder_cli.services.installation import InstallationConfig
        from openbase_coder_cli.services.runners import _resolve_binaries

        binaries = _resolve_binaries(service.name, InstallationConfig.load())
        # Each external runner executes its first declared executable. The
        # second Codex resolver is used only for launcher configuration.
        binary = Path(next(iter(binaries.values()))).resolve()
        digest = file_digest(binary)
        if not digest or not record.get("binary_sha256"):
            return unknown
        if (
            str(binary) != record.get("binary_path")
            or digest != record["binary_sha256"]
        ):
            return detail(
                service.description,
                "stale",
                "The installed executable changed after this process started.",
                action,
            )
        if record.get("launcher_digest") != file_digest(
            workspace / "cli/openbase_coder_cli/services/runners.py"
        ):
            return detail(
                service.description,
                "stale",
                "Service launcher code changed after startup.",
                action,
            )
        if service.name == "livekit-server":
            from openbase_coder_cli.services.freshness.binaries import (
                livekit_matches_pin,
            )

            matches_pin = livekit_matches_pin(binary, workspace)
            if matches_pin is None:
                return unknown
            if not matches_pin:
                return detail(
                    service.description,
                    "stale",
                    "Running LiveKit engine differs from the current source pin.",
                    "Run openbase-coder restart --service livekit-server to install the pin and restart voice services.",
                )
        return detail(
            service.description,
            "current",
            "Running executable matches the installed artifact.",
            action,
        )
    try:
        if Path(psutil.Process(pid).exe()).resolve() != Path(
            record.get("python_executable", "")
        ):
            return unknown
    except psutil.Error:
        return unknown
    sources = record.get("sources")
    if not isinstance(sources, dict):
        return unknown
    changed, missing = [], []
    for repo, _ in service.freshness_packages:
        source = sources.get(repo)
        if repo not in current:
            current[repo] = revision(workspace / repo)
        expected = current[repo]
        if not isinstance(source, dict) or not source.get("commit") or not expected:
            missing.append(repo)
        elif not source.get("matches_workspace"):
            changed.append(f"{repo}: loaded from a different checkout")
        elif source["commit"] != expected:
            changed.append(f"{repo}: {source['commit'][:8]} → {expected[:8]}")
    if changed:
        return detail(service.description, "stale", "; ".join(changed), action)
    if missing:
        return detail(
            service.description,
            "unknown",
            f"Cannot verify loaded {', '.join(missing)}.",
            action,
        )
    return detail(
        service.description, "current", "Startup matches checkout commits.", action
    )


def _collect(workspace: Path) -> dict:
    from openbase_coder_cli.services.definitions import SERVICES
    from openbase_coder_cli.services.launchd import launchctl_status
    from openbase_coder_cli.services.selection import (
        service_supports_configured_backends,
    )

    current, components = {}, []
    for service in SERVICES:
        if not service_supports_configured_backends(service):
            continue
        try:
            info = launchctl_status(service)
            if info.get("pid"):
                components.append(
                    _service_detail(service, int(info["pid"]), workspace, current)
                )
        except (OSError, ValueError, TypeError, RuntimeError, click.ClickException):
            components.append(
                detail(
                    service.description,
                    "unknown",
                    "Freshness probe unavailable.",
                    "Check service status and retry.",
                )
            )
    # Separate native processes are outside the managed-service registry. Do
    # not silently report an old helper current based on its app bundle.
    try:
        components.extend(_native_coverage())
    except psutil.Error:
        components.append(
            detail(
                "Native companions",
                "unknown",
                "Cannot inspect running native processes.",
                "Retry the freshness check.",
            )
        )
    return {"checked_at": time.time(), "components": components, "revisions": current}


def _native_coverage() -> list[dict]:
    names = {
        "OpenbaseNetmesh": "Openbase VPN app",
        "OpenbaseNetmeshCompanion": "Openbase VPN companion",
        "NetmeshHelper": "Openbase VPN helper",
        "OpenbaseScreenShareCompanion": "Screen sharing companion",
    }
    running = set()
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] in names:
            running.add(proc.info["name"])
    return [
        detail(
            names[name],
            "unknown",
            "This native component does not report source provenance yet.",
            "Rebuild/relaunch after native source changes; its app version alone cannot verify the running helper.",
        )
        for name in sorted(running)
    ]


def collect_freshness(client=None) -> dict:
    global _cache
    workspace = runtime.developer_workspace()
    if workspace is None:
        return {"enabled": False, "components": []}
    identity = workspace_id(workspace)
    with _lock:
        if (
            _cache is None
            or _cache[1] != identity
            or time.monotonic() - _cache[0] >= _CACHE_SECONDS
        ):
            _cache = (time.monotonic(), identity, _collect(workspace))
        snapshot = _cache[2]
    components = list(snapshot["components"])
    current = dict(snapshot["revisions"])
    if isinstance(client, dict):
        component = client.get("component")
        if component in ("console", "desktop"):
            components.append(
                compare_build(client.get("build"), component, workspace, current)
            )
        if component == "desktop":
            components.append(
                compare_build(client.get("main"), "desktop-main", workspace, current)
            )
    return {
        "enabled": True,
        "checked_at": snapshot["checked_at"],
        "coverage": "Commit-level checks; uncommitted edits and existing agent-session prompts are not checked. Mobile clients are not source-verified.",
        "components": components,
    }
