from __future__ import annotations

# ruff: noqa: E402, I001

import os
from dataclasses import dataclass

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import health_warnings as hw


@dataclass
class FakeService:
    name: str
    install_by_default: bool = True
    backends: tuple[str, ...] | None = None

    def supports_backend(self, coding_backend: str) -> bool:
        return self.backends is None or coding_backend in self.backends


def test_expected_service_not_running_warns(monkeypatch) -> None:
    services = [FakeService("django-cli"), FakeService("livekit-server")]
    statuses = {
        "django-cli": {"installed": True, "pid": 123},
        "livekit-server": {"installed": True, "pid": None, "last_exit_code": 1},
    }
    monkeypatch.setattr("openbase_coder_cli.services.definitions.SERVICES", services)
    monkeypatch.setattr(
        "openbase_coder_cli.services.launchd.launchctl_status",
        lambda svc: statuses[svc.name],
    )

    warnings = hw._service_warnings()

    ids = [w["id"] for w in warnings]
    assert ids == ["service-stopped:livekit-server"]
    assert warnings[0]["severity"] == "critical"


def test_conditional_service_expected_only_when_enabled(monkeypatch) -> None:
    services = [FakeService("code-sync", install_by_default=False)]
    monkeypatch.setattr("openbase_coder_cli.services.definitions.SERVICES", services)
    status = {"installed": False, "pid": None}
    monkeypatch.setattr(
        "openbase_coder_cli.services.launchd.launchctl_status", lambda svc: status
    )

    monkeypatch.setattr(hw, "_code_sync_expected", lambda: True)
    hw._CONDITIONAL_SERVICES["code-sync"] = lambda: True
    try:
        warnings = hw._service_warnings()
        assert [w["id"] for w in warnings] == ["service-missing:code-sync"]

        # Feature off + service installed -> unexpected-service warning.
        hw._CONDITIONAL_SERVICES["code-sync"] = lambda: False
        status.update({"installed": True, "pid": 5})
        warnings = hw._service_warnings()
        assert [w["id"] for w in warnings] == ["service-unexpected:code-sync"]

        # Feature off + not installed -> silence.
        status.update({"installed": False, "pid": None})
        assert hw._service_warnings() == []
    finally:
        hw._CONDITIONAL_SERVICES["code-sync"] = hw._code_sync_expected


def test_backend_scoped_service_not_expected_on_other_backend(monkeypatch) -> None:
    services = [
        FakeService("django-cli"),
        FakeService("codex-app-server", backends=("codex", "openbase_cloud_codex")),
    ]
    monkeypatch.setattr("openbase_coder_cli.services.definitions.SERVICES", services)
    monkeypatch.setattr(
        "openbase_coder_cli.services.launchd.launchctl_status",
        lambda svc: {"installed": svc.name == "django-cli", "pid": 123},
    )
    monkeypatch.setattr(
        "openbase_coder_cli.services.selection.configured_coding_backend",
        lambda: "claude_code",
    )

    assert hw._service_warnings() == []

    monkeypatch.setattr(
        "openbase_coder_cli.services.selection.configured_coding_backend",
        lambda: "codex",
    )
    assert [warning["id"] for warning in hw._service_warnings()] == [
        "service-missing:codex-app-server"
    ]


def test_collect_skips_sync_checks_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(hw, "_service_warnings", lambda: [])
    monkeypatch.setattr(hw, "_installation_warnings", lambda: [])
    monkeypatch.setattr(hw, "_livekit_skew_warnings", lambda: [])
    monkeypatch.setattr(hw, "_code_sync_expected", lambda: False)
    called = []
    monkeypatch.setattr(hw, "_sync_warnings", lambda: called.append(1) or [])

    assert hw.collect_warnings() == []
    assert called == []


def test_installation_warning_when_workspace_tracked_on_standalone(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from openbase_coder_cli.services import installation as installation_module

    monkeypatch.setattr(installation_module.InstallationConfig, "exists", lambda: True)
    monkeypatch.setattr(
        installation_module.InstallationConfig,
        "load",
        lambda: SimpleNamespace(standalone=True),
    )
    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.projects.get_recent_projects",
        lambda: [
            {"path": "/Users/u/Projects/other"},
            {"path": "/Users/u/Projects/openbase/code/openbase-coder-workspace"},
        ],
    )

    warnings = hw._installation_warnings()
    assert [w["id"] for w in warnings] == ["installation-not-dev"]

    # Dev installs never warn.
    monkeypatch.setattr(
        installation_module.InstallationConfig,
        "load",
        lambda: SimpleNamespace(standalone=False),
    )
    assert hw._installation_warnings() == []


def test_livekit_skew_warns_only_on_dev_installs(monkeypatch) -> None:
    from types import SimpleNamespace

    from openbase_coder_cli.services import installation as installation_module

    monkeypatch.setattr(installation_module.InstallationConfig, "exists", lambda: True)
    monkeypatch.setattr(
        installation_module.InstallationConfig,
        "load",
        lambda: SimpleNamespace(standalone=False),
    )
    monkeypatch.setattr(hw, "_resolve_livekit_binary", lambda: "/fake/livekit-server")

    class FakeResult:
        stdout = "livekit-server version 0.0.1\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeResult())
    warnings = hw._livekit_skew_warnings()
    assert [w["id"] for w in warnings] == ["livekit-version-skew"]
    assert "0.0.1" in warnings[0]["message"]

    from openbase_coder_cli.livekit_version import LIVEKIT_SERVER_PINNED_VERSION

    FakeResult.stdout = f"livekit-server version {LIVEKIT_SERVER_PINNED_VERSION}\n"
    assert hw._livekit_skew_warnings() == []

    # Standalone installs run the bundled pin by construction: no warning.
    monkeypatch.setattr(
        installation_module.InstallationConfig,
        "load",
        lambda: SimpleNamespace(standalone=True),
    )
    FakeResult.stdout = "livekit-server version 0.0.1\n"
    assert hw._livekit_skew_warnings() == []


def test_thread_exchange_warnings(monkeypatch, tmp_path) -> None:
    import json as json_module

    class ConnectedClient:
        def connections(self):
            return {"PEER": {"connected": True}}

    monkeypatch.setattr(
        "openbase_coder_cli.code_sync.syncthing.SyncthingClient", ConnectedClient
    )
    monkeypatch.setattr(
        "openbase_coder_cli.paths.OPENBASE_BASE_DIR", tmp_path, raising=False
    )
    monkeypatch.setattr(hw, "_thread_exchange_base", lambda: tmp_path)

    (tmp_path / "thread-sync-device.json").write_text(
        json_module.dumps({"device_id": "me-uuid"})
    )
    devices = tmp_path / "thread-sync" / "devices"

    # Nobody has exported anything: both warnings fire.
    devices.mkdir(parents=True)
    ids = [w["id"] for w in hw._thread_exchange_warnings()]
    assert ids == ["thread-sync-no-peer-snapshots", "thread-sync-not-exporting"]

    # Own exports only: peer side dead.
    (devices / "me-uuid").mkdir()
    ids = [w["id"] for w in hw._thread_exchange_warnings()]
    assert ids == ["thread-sync-no-peer-snapshots"]

    # Both sides exporting: clean.
    (devices / "them-uuid").mkdir()
    assert hw._thread_exchange_warnings() == []

    # Peer disconnected: never warn (idle machine off is normal).
    class DisconnectedClient:
        def connections(self):
            return {"PEER": {"connected": False}}

    monkeypatch.setattr(
        "openbase_coder_cli.code_sync.syncthing.SyncthingClient", DisconnectedClient
    )
    (devices / "them-uuid").rmdir()
    assert hw._thread_exchange_warnings() == []
