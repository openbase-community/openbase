"""Verify downloaded native VPN bundles that this workspace cannot rebuild.

Public checkouts have no netmesh-macos source: the stage scripts download the
signed prebuilt companion and menu-bar app instead. Their provenance manifest
names the release build's workspace, so a source comparison would always read
stale. Here a running process is matched to the bundle setup downloaded by its
loaded image UUID, and that bundle is verified by code signature and build
number against the desktop runtime's minimum-build contract.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path

from openbase_coder_cli.services.freshness.compare import detail
from openbase_coder_cli.services.freshness.source import read_manifest
from openbase_coder_cli.services.netmesh_companion import (
    NETMESH_BUNDLE_IDENTIFIER,
    NETMESH_TEAM_IDENTIFIER,
    netmesh_source_checkout,
)

ACTION = (
    "Re-run the Openbase VPN download step (pnpm --dir desktop companion:stage:netmesh "
    "and companion:stage:netmesh-menubar), update its helper, and relaunch the native apps."
)
COVERAGE = (
    "Downloaded Openbase VPN components are verified by code signature and "
    "build number, not source."
)
# The helper is registered from whichever bundle launched it.
BUNDLES = {
    "OpenbaseNetmesh": ("OpenbaseNetmesh.app",),
    "OpenbaseNetmeshCompanion": ("OpenbaseNetmeshCompanion.app",),
    "NetmeshHelper": ("OpenbaseNetmeshCompanion.app", "OpenbaseNetmesh.app"),
}
MANIFEST = "Contents/Resources/openbase-native-provenance.json"


def can_rebuild(workspace: Path) -> bool:
    """True when the stage scripts would build netmesh from source here."""
    return netmesh_source_checkout(workspace) is not None


def minimum_build(workspace: Path) -> int | None:
    """The desktop runtime's prebuilt floor, read from the current source."""
    contract = workspace / "desktop/scripts/netmesh-prebuilt-contract.mjs"
    try:
        match = re.search(
            r"^export const MINIMUM_NETMESH_BUILD = (\d+);", contract.read_text(), re.M
        )
    except OSError:
        return None
    return int(match.group(1)) if match else None


def signature(app: Path) -> dict[str, str] | None:
    try:
        result = subprocess.run(
            ["codesign", "-d", "--verbose=2", str(app)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return dict(re.findall(r"^(Identifier|TeamIdentifier)=(.+)$", result.stderr, re.M))


def read_bundle(app: Path) -> dict | None:
    if not app.is_dir():
        return None
    manifest = read_manifest(app / MANIFEST) or {}
    images = manifest.get("image_uuids")
    try:
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        build = int(str(info.get("CFBundleVersion", "")).strip())
    except (OSError, ValueError, plistlib.InvalidFileException):
        build = None
    return {
        "image_uuids": images if isinstance(images, dict) else {},
        "build": build,
        "signature": signature(app),
    }


def compare_prebuilt(label: str, name: str, image_uuid: str, workspace: Path) -> dict:
    """Judge a process whose startup identity has already been verified."""
    staged = [
        read_bundle(workspace / "desktop/companion-build" / app)
        for app in BUNDLES[name]
    ]
    loaded = next(
        (
            bundle
            for bundle in staged
            if bundle and image_uuid in (bundle["image_uuids"].get(name) or [])
        ),
        None,
    )
    if loaded is None:
        if not any(staged):
            return detail(
                label,
                "unknown",
                "No downloaded Openbase VPN bundle to verify against.",
                ACTION,
            )
        return detail(
            label,
            "stale",
            "The downloaded bundle changed after this process started.",
            ACTION,
        )
    signed = loaded["signature"] or {}
    if (
        signed.get("Identifier") != NETMESH_BUNDLE_IDENTIFIER
        or signed.get("TeamIdentifier") != NETMESH_TEAM_IDENTIFIER
    ):
        return detail(
            label,
            "unknown",
            "The downloaded bundle is not signed as an Openbase VPN release.",
            ACTION,
        )
    expected = minimum_build(workspace)
    build = loaded["build"]
    if expected is None or build is None:
        return detail(
            label, "unknown", "Cannot read the expected prebuilt build number.", ACTION
        )
    if build < expected:
        return detail(
            label,
            "stale",
            f"Downloaded build {build} is older than the required build {expected}.",
            ACTION,
        )
    return detail(
        label,
        "current",
        f"Signed prebuilt build {build} matches the downloaded bundle.",
        ACTION,
    )
