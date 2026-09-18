"""Capture once before exec; never reconstruct loaded code from current HEAD."""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from contextlib import suppress
from pathlib import Path

import psutil

from openbase_coder_cli.paths import OPENBASE_BASE_DIR
from openbase_coder_cli.services.freshness.source import (
    SCHEMA_VERSION,
    file_digest,
    read_manifest,
    revision,
    workspace_id,
    write_manifest,
)
from openbase_coder_cli.services.installation import InstallationConfig

RECORD_DIR = OPENBASE_BASE_DIR / "dev-runtime"


def developer_workspace(config: InstallationConfig | None = None) -> Path | None:
    from openbase_coder_cli.runtime import is_standalone_runtime

    if is_standalone_runtime():
        return None
    if config is None:
        if not InstallationConfig.exists():
            return None
        try:
            config = InstallationConfig.load()
        except (OSError, ValueError, TypeError):
            return None
    if config.standalone or not config.workspace_path:
        return None
    workspace = Path(config.workspace_path).expanduser().resolve()
    # A vanished checkout is still a developer install, but all evidence
    # becomes unknown. Do not silently disable its warning.
    return workspace


def process_identity(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def _package_repo(module: str) -> Path | None:
    spec = importlib.util.find_spec(module)
    if not spec or not spec.origin:
        return None
    origin = Path(spec.origin).resolve()
    return next((p for p in origin.parents if (p / ".git").exists()), None)


def capture_service(name: str, config: InstallationConfig, argv: list[str]) -> None:
    """Record this runner's provenance before exec preserves its PID/start time.

    Any failed/unsupported capture removes the old record. The collector then
    reports unknown instead of blocking an otherwise healthy service.
    """
    workspace = developer_workspace(config)
    if workspace is None:
        return
    from openbase_coder_cli.services.definitions import SERVICES

    service = next(item for item in SERVICES if item.name == name)
    sources = {}
    for repo, module in service.freshness_packages:
        root = _package_repo(module)
        sources[repo] = {
            "commit": revision(root) if root else None,
            "matches_workspace": root == (workspace / repo).resolve(),
        }
    binary = Path(argv[0]).resolve()
    artifact = None
    if service.freshness_kind == "build":
        artifact = read_manifest(binary.with_name(binary.name + ".provenance.json"))
        if artifact and artifact.get("artifact_sha256") != file_digest(binary):
            artifact = None
    record = {
        "schema_version": SCHEMA_VERSION,
        "workspace_id": workspace_id(workspace),
        "pid": os.getpid(),
        "process_start": process_identity(os.getpid()),
        "captured_at": time.time(),
        "sources": sources,
        "kind": service.freshness_kind,
        "launcher_digest": file_digest(
            workspace / "cli/openbase_coder_cli/services/runners.py"
        ),
        "binary_path": str(binary),
        "python_executable": str(Path(sys.executable).resolve()),
        "binary_sha256": file_digest(binary)
        if service.freshness_kind != "python"
        else None,
        "artifact": artifact,
    }
    path = RECORD_DIR / f"{name}.json"
    try:
        write_manifest(path, record)
    except OSError:
        # Never leave an older record looking like a successful new capture.
        with suppress(OSError):
            path.unlink(missing_ok=True)
