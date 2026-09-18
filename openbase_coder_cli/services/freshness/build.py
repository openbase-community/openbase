"""Provenance sidecars for locally compiled native artifacts."""

from pathlib import Path

from openbase_coder_cli.services.freshness.source import (
    SCHEMA_VERSION,
    file_digest,
    revision,
    workspace_id,
    write_manifest,
)


def capture_build(workspace: Path, component: str, repos: tuple[str, ...]) -> dict:
    revisions = {repo: revision(workspace / repo) for repo in repos}
    return {
        "schema_version": SCHEMA_VERSION,
        "component": component,
        "workspace_id": workspace_id(workspace),
        "revisions": revisions,
        "verified": all(revisions.values()),
    }


def finish_build(binary: Path, workspace: Path, initial: dict) -> None:
    final = capture_build(workspace, initial["component"], tuple(initial["revisions"]))
    value = {
        **initial,
        "verified": initial == final and initial["verified"],
        "artifact_sha256": file_digest(binary),
    }
    write_manifest(binary.with_name(binary.name + ".provenance.json"), value)
