import copy
import os
import subprocess

import pytest

from openbase_coder_cli.services.freshness import native, runtime
from openbase_coder_cli.services.freshness.build import capture_build


@pytest.fixture
def evidence(tmp_path):
    repo = tmp_path / "netmesh-macos"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "Initial")
    name = "OpenbaseNetmesh"
    build = capture_build(tmp_path, name, ("netmesh-macos",))
    build["image_uuids"] = {name: ["11111111-2222-3333-4444-555555555555"]}
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
    if change == "pid": record["pid"] += 1
    if change == "start": record["process_start"] -= 1
    if change == "component": record["component"] = "NetmeshHelper"
    if change == "uuid": record["image_uuid"] = "other-image"
    if change == "unverified": record["build"]["verified"] = False
    if change == "missing": record = None
    if change == "schema": record["schema_version"] = 99
    if change == "nan": record["process_start"] = float("nan")
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
