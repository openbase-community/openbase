"""Validate and compare client build stamps without accepting client paths."""

from __future__ import annotations

from pathlib import Path

from openbase_coder_cli.services.freshness.source import (
    SCHEMA_VERSION,
    revision,
    workspace_id,
)

RENDERER_REPOS = ("coder-react", "multi-react", "boilersync-react")
COMPONENT_REPOS = {
    "desktop": ("desktop", *RENDERER_REPOS),
    "console": ("console", *RENDERER_REPOS),
    "desktop-main": ("desktop",),
    "openbase-tunneld": ("cli",),
    "OpenbaseNetmesh": ("netmesh-macos",),
    "OpenbaseNetmeshCompanion": ("netmesh-macos",),
    "NetmeshHelper": ("netmesh-macos",),
}


def detail(component: str, state: str, reason: str, action: str, **extra) -> dict:
    return {
        "component": component,
        "state": state,
        "reason": reason,
        "action": action,
        **extra,
    }


def compare_build(manifest, component: str, workspace: Path, current: dict) -> dict:
    label = {
        "desktop": "Desktop UI",
        "console": "Web console",
        "desktop-main": "Desktop app",
    }.get(component, component)
    action = {
        "desktop": "Rebuild desktop, then reload the window.",
        "console": "Rebuild console, then reload this page.",
        "desktop-main": "Rebuild if packaged, then quit and relaunch the developer app.",
        "openbase-tunneld": "Run openbase-coder restart --service openbase-tunneld to rebuild and restart Openbase Direct.",
    }.get(component, "Rebuild and relaunch the native component.")
    unknown = detail(
        label,
        "unknown",
        "No verified build stamp. Rebuild to enable freshness checks.",
        action,
    )
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != SCHEMA_VERSION
    ):
        return unknown
    if manifest.get("component") != component or manifest.get("verified") is not True:
        return unknown
    if manifest.get("workspace_id") != workspace_id(workspace):
        return detail(
            label, "stale", "Built from a different source workspace.", action
        )
    sources = manifest.get("revisions")
    if not isinstance(sources, dict):
        return unknown
    changes = []
    missing = []
    for repo in COMPONENT_REPOS[component]:
        expected = (
            current.setdefault(repo, revision(workspace / repo))
            if repo not in current
            else current[repo]
        )
        loaded = sources.get(repo)
        if not isinstance(loaded, str) or len(loaded) != 40 or not expected:
            missing.append(repo)
        elif loaded != expected:
            changes.append(f"{repo}: {loaded[:8]} → {expected[:8]}")
    if changes:
        return detail(label, "stale", "; ".join(changes), action)
    if missing:
        return detail(label, "unknown", f"Cannot verify {', '.join(missing)}.", action)
    return detail(label, "current", "Loaded build matches checkout commits.", action)
