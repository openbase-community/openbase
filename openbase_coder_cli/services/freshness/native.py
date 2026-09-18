"""Read native startup evidence without inferring loaded code from app bundles."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import psutil

from openbase_coder_cli.services.freshness import runtime
from openbase_coder_cli.services.freshness.compare import compare_build, detail
from openbase_coder_cli.services.freshness.source import read_manifest

NAMES = {
    "OpenbaseNetmesh": "Openbase VPN app",
    "OpenbaseNetmeshCompanion": "Openbase VPN companion",
    "NetmeshHelper": "Openbase VPN helper",
    "OpenbaseScreenShareCompanion": "Screen sharing companion",
}
ACTION = "Rebuild the native VPN app/companion, update its helper, and relaunch the native apps."


def helper_record(workspace: Path) -> dict | None:
    from openbase_coder_cli.services.netmesh_companion import netmesh_ctl_path

    ctl = netmesh_ctl_path(workspace)
    if not ctl:
        return None
    try:
        result = subprocess.run(
            [ctl, "provenance"], capture_output=True, text=True, timeout=3, check=False
        )
        if result.returncode or len(result.stdout) > 65536:
            return None
        value = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None  # Old/unreachable helpers remain unknown; never assume current.
    return value if isinstance(value, dict) else None


def compare_native(record, name: str, pid: int, workspace: Path, current: dict) -> dict:
    label = NAMES[name]
    unknown = detail(label, "unknown", "Running native process has no verified startup provenance.", ACTION)
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        return unknown
    if record.get("component") != name or record.get("pid") != pid:
        return unknown
    actual_start = runtime.process_identity(pid)
    claimed_start = record.get("process_start")
    if (
        actual_start is None
        or not isinstance(claimed_start, (int, float))
        or not math.isfinite(claimed_start)
        or abs(actual_start - claimed_start) > 0.000001
    ):
        return unknown
    build = record.get("build")
    images = build.get("image_uuids") if isinstance(build, dict) else None
    uuids = images.get(name) if isinstance(images, dict) else None
    loaded_uuid = record.get("image_uuid")
    if not isinstance(uuids, list) or not isinstance(loaded_uuid, str) or loaded_uuid not in uuids:
        return unknown
    # The native process already matched this manifest against its in-memory
    # LC_UUID at startup. Reading a newly rebuilt on-disk binary here would
    # incorrectly clear a still-running old process.
    result = compare_build({**build, "component": name}, name, workspace, current)
    return {**result, "component": label, "action": ACTION}


def collect_native(workspace: Path, current: dict) -> list[dict]:
    components = []
    helper = None
    for process in psutil.process_iter(["name"]):
        name = process.info["name"]
        if name not in NAMES:
            continue
        if name == "OpenbaseScreenShareCompanion":
            components.append(detail(NAMES[name], "unknown",
                "This native component does not report source provenance yet.",
                "Rebuild/relaunch after native source changes; source verification requires native provenance support."))
            continue
        if name == "NetmeshHelper":
            if helper is None:
                helper = helper_record(workspace) or {}
            record = helper
        else:
            record = read_manifest(runtime.RECORD_DIR / f"native-{name}-{process.pid}.json")
        components.append(compare_native(record, name, process.pid, workspace, current))
    return components
