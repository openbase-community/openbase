from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from openbase_coder_cli.services.definitions import SERVICES
from openbase_coder_cli.services.freshness import collector, runtime
from openbase_coder_cli.services.freshness.build import capture_build, finish_build
from openbase_coder_cli.services.freshness.compare import compare_build
from openbase_coder_cli.services.freshness.source import (
    read_manifest,
    revision,
    workspace_id,
)
from openbase_coder_cli.services.installation import InstallationConfig


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo, content):
    (repo / "code.py").write_text(content)
    git(repo, "add", "code.py")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "Change source",
    )
    return revision(repo)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "multi.json").write_text("{}")
    for name in (
        "cli",
        "super-agents",
        "desktop",
        "console",
        "coder-react",
        "multi-react",
        "boilersync-react",
    ):
        repo = tmp_path / name
        repo.mkdir()
        git(repo, "init", "-q")
        commit(repo, "initial")
    return tmp_path


def test_live_process_stays_stale_until_replaced(workspace, monkeypatch):
    records = workspace / "records"
    monkeypatch.setattr(runtime, "RECORD_DIR", records)
    service = next(s for s in SERVICES if s.name == "livekit-agent")
    # A real child preserves its own frozen capture while the parent advances
    # Git; no platform service manager or user installation is modified.
    script = """
import sys, time
from pathlib import Path
from openbase_coder_cli.services.freshness import runtime
from openbase_coder_cli.services.installation import InstallationConfig
w = Path(sys.argv[1])
runtime.RECORD_DIR = w / "records"
runtime._package_repo = lambda module: w / ("cli" if module == "openbase_coder_cli" else "super-agents")
runtime.capture_service("livekit-agent", InstallationConfig(workspace_path=str(w)), [sys.executable])
print("ready", flush=True)
time.sleep(60)
"""

    def launch():
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(workspace)],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout.readline().strip() == "ready"
        return proc

    process = launch()
    try:
        assert (
            collector._service_detail(service, process.pid, workspace, {})["state"]
            == "current"
        )
        commit(workspace / "super-agents", "dependency changed")
        result = collector._service_detail(service, process.pid, workspace, {})
        assert result["state"] == "stale"
        assert "super-agents" in result["reason"]
        process.terminate()
        process.wait(timeout=5)
        assert (
            collector._service_detail(service, process.pid, workspace, {})["state"]
            == "unknown"
        )
        process = launch()
        assert (
            collector._service_detail(service, process.pid, workspace, {})["state"]
            == "current"
        )
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_old_compiled_artifact_remains_stale_after_restart(workspace, monkeypatch):
    binary = workspace / "tunneld"
    binary.write_bytes(b"compiled-A")
    initial = capture_build(workspace, "openbase-tunneld", ("cli",))
    finish_build(binary, workspace, initial)
    commit(workspace / "cli", "new source")
    monkeypatch.setattr(runtime, "RECORD_DIR", workspace / "records")
    runtime.capture_service(
        "openbase-tunneld",
        InstallationConfig(workspace_path=str(workspace)),
        [str(binary)],
    )
    service = next(s for s in SERVICES if s.name == "openbase-tunneld")
    assert (
        collector._service_detail(service, os.getpid(), workspace, {})["state"]
        == "stale"
    )
    # A mismatched sidecar must not verify a replaced executable.
    binary.write_bytes(b"something else")
    runtime.capture_service(
        "openbase-tunneld",
        InstallationConfig(workspace_path=str(workspace)),
        [str(binary)],
    )
    assert (
        collector._service_detail(service, os.getpid(), workspace, {})["state"]
        == "unknown"
    )


def test_rebuild_does_not_refresh_loaded_renderer(workspace):
    repos = ("desktop", "coder-react", "multi-react", "boilersync-react")
    loaded = capture_build(workspace, "desktop", repos)
    commit(workspace / "coder-react", "new shared renderer")
    rebuilt = capture_build(workspace, "desktop", repos)
    assert compare_build(loaded, "desktop", workspace, {})["state"] == "stale"
    assert compare_build(rebuilt, "desktop", workspace, {})["state"] == "current"


def test_build_race_unknown(workspace):
    binary = workspace / "tunneld"
    binary.write_bytes(b"compiled")
    stamp = capture_build(workspace, "openbase-tunneld", ("cli",))
    commit(workspace / "cli", "changed during build")
    finish_build(binary, workspace, stamp)
    actual = read_manifest(workspace / "tunneld.provenance.json")
    assert (
        compare_build(actual, "openbase-tunneld", workspace, {})["state"] == "unknown"
    )


def test_pid_reuse_and_wrong_workspace_do_not_pass(workspace, monkeypatch):
    monkeypatch.setattr(runtime, "RECORD_DIR", workspace / "records")
    monkeypatch.setattr(
        runtime,
        "_package_repo",
        lambda module: (
            workspace / ("cli" if module == "openbase_coder_cli" else "super-agents")
        ),
    )
    config = InstallationConfig(workspace_path=str(workspace))
    runtime.capture_service("livekit-agent", config, [sys.executable])
    service = next(s for s in SERVICES if s.name == "livekit-agent")
    record_file = runtime.RECORD_DIR / "livekit-agent.json"
    record = json.loads(record_file.read_text())
    record["process_start"] -= 1
    record_file.write_text(json.dumps(record))
    assert (
        collector._service_detail(service, os.getpid(), workspace, {})["state"]
        == "unknown"
    )
    record["process_start"] = runtime.process_identity(os.getpid())
    record["workspace_id"] = "different"
    record_file.write_text(json.dumps(record))
    assert (
        collector._service_detail(service, os.getpid(), workspace, {})["state"]
        == "stale"
    )


def test_missing_unknown_schema_and_hostile_client_input(workspace):
    for manifest in (
        None,
        [],
        {"schema_version": 99},
        {
            "schema_version": 1,
            "verified": True,
            "component": "desktop",
            "workspace_id": workspace_id(workspace),
            "revisions": [],
        },
    ):
        assert compare_build(manifest, "desktop", workspace, {})["state"] == "unknown"


def test_source_in_nested_non_repo_is_unknown(workspace):
    folder = workspace / "cli" / "nested"
    folder.mkdir()
    assert revision(folder) is None


def test_production_does_not_capture_or_collect(workspace, monkeypatch):
    monkeypatch.setattr(runtime, "RECORD_DIR", workspace / "records")
    monkeypatch.setattr(
        "openbase_coder_cli.runtime.is_standalone_runtime", lambda: True
    )
    monkeypatch.setattr(
        collector, "_collect", lambda *_: pytest.fail("production scanned source")
    )
    runtime.capture_service(
        "django-cli",
        InstallationConfig(workspace_path=str(workspace)),
        [sys.executable],
    )
    assert not runtime.RECORD_DIR.exists()
    assert collector.collect_freshness({"component": "desktop"}) == {
        "enabled": False,
        "components": [],
    }


def test_stopped_and_inactive_services_are_not_freshness_failures(
    workspace, monkeypatch
):
    monkeypatch.setattr(
        "openbase_coder_cli.services.launchd.launchctl_status", lambda _: {"pid": None}
    )
    monkeypatch.setattr(collector, "_native_coverage", lambda: [])
    assert collector._collect(workspace)["components"] == []


def test_livekit_pin_reads_updated_source_without_reimport(workspace):
    from openbase_coder_cli.services.freshness.binaries import livekit_matches_pin

    pin = workspace / "cli/openbase_coder_cli/livekit_version.py"
    pin.parent.mkdir()
    pin.write_text('LIVEKIT_SERVER_PINNED_VERSION = "1.0.0"\n')
    binary = workspace / "livekit-server"
    binary.write_text('#!/bin/sh\necho "livekit-server version 1.0.0"\n')
    binary.chmod(0o755)
    assert livekit_matches_pin(binary, workspace) is True
    pin.write_text('LIVEKIT_SERVER_PINNED_VERSION = "2.0.0"\n')
    assert livekit_matches_pin(binary, workspace) is False


def test_external_binary_replacement_requires_restart(workspace, monkeypatch):
    binary = workspace / "codex"
    binary.write_bytes(b"binary-A")
    monkeypatch.setattr(runtime, "RECORD_DIR", workspace / "records")
    monkeypatch.setattr("openbase_coder_cli.services.runners._resolve_binaries", lambda *_: {"codex": str(binary)})
    monkeypatch.setattr(InstallationConfig, "load", lambda: InstallationConfig(workspace_path=str(workspace)))
    runtime.capture_service("codex-app-server", InstallationConfig(workspace_path=str(workspace)), [str(binary)])
    binary.write_bytes(b"binary-B")
    service = next(s for s in SERVICES if s.name == "codex-app-server")
    assert collector._service_detail(service, os.getpid(), workspace, {})["state"] == "stale"


def test_unstamped_compiled_service_requires_rebuild(workspace, monkeypatch):
    monkeypatch.setattr(runtime, "RECORD_DIR", workspace / "missing")
    service = next(s for s in SERVICES if s.name == "openbase-tunneld")
    result = collector._service_detail(service, os.getpid(), workspace, {})
    assert result["state"] == "unknown"
    assert "Rebuild/install" in result["action"]
