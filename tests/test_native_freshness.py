import copy
import json
import os
import plistlib
import subprocess

import pytest

from openbase_coder_cli.services.freshness import native, prebuilt, runtime
from openbase_coder_cli.services.freshness.build import capture_build
from openbase_coder_cli.services.netmesh_companion import (
    NETMESH_BUNDLE_IDENTIFIER,
    NETMESH_TEAM_IDENTIFIER,
)

UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def evidence(tmp_path):
    repo = tmp_path / "netmesh-macos"
    repo.mkdir()
    (repo / "project.yml").write_text("name: OpenbaseNetmesh\n")  # Source checkout: rebuildable.
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "Initial")
    name = "OpenbaseNetmesh"
    build = capture_build(tmp_path, name, ("netmesh-macos",))
    build["image_uuids"] = {name: [UUID]}
    record = {"schema_version": 1, "component": name, "pid": os.getpid(),
              "process_start": runtime.process_identity(os.getpid()),
              "image_uuid": build["image_uuids"][name][0], "build": build}
    return tmp_path, record, git


def test_native_rebuild_does_not_refresh_running_process(evidence):
    workspace, record, git = evidence
    def compare(value):
        return native.compare_native(value, "OpenbaseNetmesh", os.getpid(), workspace, {})
    assert compare(record)["state"] == "current"
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "New source")
    assert compare(record)["state"] == "stale"
    rebuilt = copy.deepcopy(record)
    rebuilt["build"]["revisions"]["netmesh-macos"] = git("rev-parse", "HEAD")
    assert compare(rebuilt)["state"] == "current"
    assert compare(record)["state"] == "stale"


@pytest.mark.parametrize("change", ["pid", "start", "component", "uuid", "unverified", "missing", "schema", "nan"])
def test_invalid_native_evidence_never_reports_current(evidence, change):
    workspace, record, _ = evidence
    if change == "pid":
        record["pid"] += 1
    if change == "start":
        record["process_start"] -= 1
    if change == "component":
        record["component"] = "NetmeshHelper"
    if change == "uuid":
        record["image_uuid"] = "other-image"
    if change == "unverified":
        record["build"]["verified"] = False
    if change == "missing":
        record = None
    if change == "schema":
        record["schema_version"] = 99
    if change == "nan":
        record["process_start"] = float("nan")
    assert native.compare_native(record, "OpenbaseNetmesh", os.getpid(), workspace, {})["state"] == "unknown"


def test_other_native_workspace_is_stale(evidence):
    workspace, record, _ = evidence
    record["build"]["workspace_id"] = "other"
    assert native.compare_native(record, "OpenbaseNetmesh", os.getpid(), workspace, {})["state"] == "stale"


def test_helper_timeout_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr("openbase_coder_cli.services.netmesh_companion.netmesh_ctl_path", lambda _: "netmesh-ctl")
    def timeout(*_args, **kwargs):
        assert kwargs["timeout"] == 3
        raise subprocess.TimeoutExpired("netmesh-ctl", 3)
    monkeypatch.setattr(native.subprocess, "run", timeout)
    assert native.helper_record(tmp_path) is None


def stage_bundle(workspace, app, *, build=18, uuids=None):
    bundle = workspace / "desktop/companion-build" / app
    (bundle / "Contents/Resources").mkdir(parents=True)
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleVersion": str(build)}))
    (bundle / "Contents/Resources/openbase-native-provenance.json").write_text(json.dumps({
        "schema_version": 1, "workspace_id": "release-laptop", "verified": True,
        "revisions": {"netmesh-macos": "a" * 40},
        "image_uuids": uuids if uuids is not None else {"OpenbaseNetmeshCompanion": [UUID], "NetmeshHelper": [UUID]},
    }))
    return bundle


@pytest.fixture
def downloaded(tmp_path, monkeypatch):
    """A public checkout: no netmesh-macos source, a downloaded companion, build floor 18."""
    scripts = tmp_path / "desktop/scripts"
    scripts.mkdir(parents=True)
    (scripts / "netmesh-prebuilt-contract.mjs").write_text("export const MINIMUM_NETMESH_BUILD = 18;\n")
    signatures = {}
    monkeypatch.setattr(prebuilt, "signature", lambda app: signatures.get(app.name, {
        "Identifier": NETMESH_BUNDLE_IDENTIFIER, "TeamIdentifier": NETMESH_TEAM_IDENTIFIER}))
    name = "NetmeshHelper"
    record = {"schema_version": 1, "component": name, "pid": os.getpid(),
              "process_start": runtime.process_identity(os.getpid()), "image_uuid": UUID,
              "build": {"schema_version": 1, "workspace_id": "release-laptop", "verified": True,
                        "revisions": {"netmesh-macos": "a" * 40}, "image_uuids": {name: [UUID]}}}
    return tmp_path, record, signatures


def compare_helper(workspace, record):
    return native.compare_native(record, "NetmeshHelper", os.getpid(), workspace, {})


def test_downloaded_prebuilt_from_another_workspace_is_current(downloaded):
    workspace, record, _ = downloaded
    stage_bundle(workspace, "OpenbaseNetmeshCompanion.app")
    result = compare_helper(workspace, record)
    assert result["state"] == "current"
    assert result["component"] == "Openbase VPN helper"
    assert "build 18" in result["reason"]
    assert "Rebuild" not in result["action"] and "download" in result["action"]
    assert native.coverage_note(workspace) == prebuilt.COVERAGE


def test_helper_loaded_from_menu_bar_bundle_is_current(downloaded):
    workspace, record, _ = downloaded
    stage_bundle(workspace, "OpenbaseNetmeshCompanion.app", uuids={"NetmeshHelper": ["other"]})
    stage_bundle(workspace, "OpenbaseNetmesh.app", uuids={"OpenbaseNetmesh": ["x"], "NetmeshHelper": [UUID]})
    assert compare_helper(workspace, record)["state"] == "current"


def test_outdated_download_is_stale_with_redownload_advice(downloaded):
    workspace, record, _ = downloaded
    stage_bundle(workspace, "OpenbaseNetmeshCompanion.app", build=17)
    result = compare_helper(workspace, record)
    assert result["state"] == "stale"
    assert "17" in result["reason"] and "18" in result["reason"]
    assert result["action"] == prebuilt.ACTION


def test_replaced_download_leaves_running_process_stale(downloaded):
    workspace, record, _ = downloaded
    stage_bundle(workspace, "OpenbaseNetmeshCompanion.app", uuids={"NetmeshHelper": ["newer-download"]})
    result = compare_helper(workspace, record)
    assert result["state"] == "stale"
    assert "changed after this process started" in result["reason"]


@pytest.mark.parametrize("problem", ["team", "identifier", "unsigned", "missing-bundle", "no-contract"])
def test_unverifiable_download_is_unknown_never_current(downloaded, problem):
    workspace, record, signatures = downloaded
    if problem != "missing-bundle":
        stage_bundle(workspace, "OpenbaseNetmeshCompanion.app")
    if problem == "team":
        signatures["OpenbaseNetmeshCompanion.app"] = {"Identifier": NETMESH_BUNDLE_IDENTIFIER, "TeamIdentifier": "ATTACKER01"}
    if problem == "identifier":
        signatures["OpenbaseNetmeshCompanion.app"] = {"Identifier": "cloud.example.other", "TeamIdentifier": NETMESH_TEAM_IDENTIFIER}
    if problem == "unsigned":
        signatures["OpenbaseNetmeshCompanion.app"] = None
    if problem == "no-contract":
        (workspace / "desktop/scripts/netmesh-prebuilt-contract.mjs").unlink()
    result = compare_helper(workspace, record)
    assert result["state"] == "unknown"
    assert result["action"] == prebuilt.ACTION


def test_download_mode_keeps_process_identity_checks(downloaded):
    workspace, record, _ = downloaded
    stage_bundle(workspace, "OpenbaseNetmeshCompanion.app")
    record["pid"] += 1
    assert compare_helper(workspace, record)["state"] == "unknown"
    record["pid"] -= 1
    record["image_uuid"] = "not-the-loaded-image"
    assert compare_helper(workspace, record)["state"] == "unknown"


def test_source_checkout_keeps_rebuild_advice(evidence):
    workspace, record, _ = evidence
    assert native.coverage_note(workspace) == ""
    record["build"]["workspace_id"] = "release-laptop"
    result = native.compare_native(record, "OpenbaseNetmesh", os.getpid(), workspace, {})
    assert result["state"] == "stale" and result["action"] == native.ACTION


def test_minimum_build_reads_current_source(tmp_path):
    assert prebuilt.minimum_build(tmp_path) is None
    (tmp_path / "desktop/scripts").mkdir(parents=True)
    (tmp_path / "desktop/scripts/netmesh-prebuilt-contract.mjs").write_text(
        "// comment\nexport const MINIMUM_NETMESH_BUILD = 21;\n")
    assert prebuilt.minimum_build(tmp_path) == 21
