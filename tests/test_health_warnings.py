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


def test_shared_codex_daemon_satisfies_stopped_openbase_service(monkeypatch) -> None:
    from openbase_coder_cli import codex_control_plane

    services = [FakeService("codex-app-server", backends=("codex",))]
    monkeypatch.setattr("openbase_coder_cli.services.definitions.SERVICES", services)
    monkeypatch.setattr(
        "openbase_coder_cli.services.launchd.launchctl_status",
        lambda _service: {"installed": True, "pid": None, "last_exit_code": 1},
    )
    monkeypatch.setattr(
        "openbase_coder_cli.services.selection.configured_coding_backend",
        lambda: "codex",
    )
    monkeypatch.setattr(codex_control_plane, "shared_codex_daemon_ready", lambda: True)

    assert hw._service_warnings() == []


def test_conditional_service_expected_only_when_enabled(monkeypatch) -> None:
    services = [FakeService("sync-daemon", install_by_default=False)]
    monkeypatch.setattr("openbase_coder_cli.services.definitions.SERVICES", services)
    status = {"installed": False, "pid": None}
    monkeypatch.setattr(
        "openbase_coder_cli.services.launchd.launchctl_status", lambda svc: status
    )

    hw._CONDITIONAL_SERVICES["sync-daemon"] = lambda: True
    try:
        warnings = hw._service_warnings()
        assert [w["id"] for w in warnings] == ["service-missing:sync-daemon"]

        # Feature off + service installed -> unexpected-service warning.
        hw._CONDITIONAL_SERVICES["sync-daemon"] = lambda: False
        status.update({"installed": True, "pid": 5})
        warnings = hw._service_warnings()
        assert [w["id"] for w in warnings] == ["service-unexpected:sync-daemon"]

        # Feature off + not installed -> silence.
        status.update({"installed": False, "pid": None})
        assert hw._service_warnings() == []
    finally:
        hw._CONDITIONAL_SERVICES["sync-daemon"] = hw._sync_daemon_expected


def test_code_sync_service_is_no_longer_expected() -> None:
    assert "code-sync" not in hw._CONDITIONAL_SERVICES


def test_backend_scoped_service_not_expected_on_other_backend(monkeypatch) -> None:
    from openbase_coder_cli import codex_control_plane

    monkeypatch.setattr(codex_control_plane, "shared_codex_daemon_ready", lambda: False)
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


def test_collect_skips_sync_checks_when_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(hw, "_service_warnings", lambda: [])
    monkeypatch.setattr(hw, "_installation_warnings", lambda: [])
    monkeypatch.setattr(hw, "_livekit_skew_warnings", lambda: [])
    monkeypatch.setattr(hw, "_codex_version_skew_warnings", lambda: [])
    monkeypatch.setattr(hw, "_sync_daemon_expected", lambda: False)
    called = []
    monkeypatch.setattr(hw, "_sync_daemon_warnings", lambda: called.append(1) or [])
    monkeypatch.setattr(
        hw, "_thread_exchange_warnings", lambda: called.append(2) or []
    )

    assert hw.collect_warnings() == []
    assert called == []


def test_collect_runs_daemon_and_thread_checks_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(hw, "_service_warnings", lambda: [])
    monkeypatch.setattr(hw, "_installation_warnings", lambda: [])
    monkeypatch.setattr(hw, "_livekit_skew_warnings", lambda: [])
    monkeypatch.setattr(hw, "_codex_version_skew_warnings", lambda: [])
    monkeypatch.setattr(hw, "_sync_daemon_expected", lambda: True)
    monkeypatch.setattr(
        hw, "_sync_daemon_warnings", lambda: [{"id": "sync-daemon-no-peer"}]
    )
    monkeypatch.setattr(
        hw, "_thread_exchange_warnings", lambda: [{"id": "thread-sync-x"}]
    )

    assert [w["id"] for w in hw.collect_warnings()] == [
        "sync-daemon-no-peer",
        "thread-sync-x",
    ]


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


def test_livekit_skew_resolver_skips_stale_download(tmp_path, monkeypatch) -> None:
    stale = tmp_path / "openbase" / "bin" / "livekit-server"
    stale.parent.mkdir(parents=True)
    stale.write_text("#!/bin/sh\n", encoding="utf-8")
    stale.chmod(0o755)
    fallback = tmp_path / "homebrew" / "livekit-server"
    fallback.parent.mkdir()
    fallback.write_text("#!/bin/sh\n", encoding="utf-8")
    fallback.chmod(0o755)

    monkeypatch.setattr(
        "openbase_coder_cli.livekit_install.installed_livekit_server_path",
        lambda: stale,
    )
    monkeypatch.setattr(
        "openbase_coder_cli.livekit_install.livekit_binary_matches_pin",
        lambda _binary: False,
    )
    monkeypatch.setattr(
        "openbase_coder_cli.livekit_install.fallback_livekit_server_path",
        lambda: fallback,
    )

    assert hw._resolve_livekit_binary() == str(fallback)


def test_thread_exchange_warnings(monkeypatch, tmp_path) -> None:
    import json as json_module

    from openbase_coder_cli import sync_daemon

    peers: list[dict] = [{"device": "mini"}]
    monkeypatch.setattr(
        sync_daemon.SyncDaemonClient, "status", lambda self: {"peers": peers}
    )
    monkeypatch.setattr(hw, "_thread_exchange_base", lambda: tmp_path)

    (tmp_path / "thread-sync-device.json").write_text(
        json_module.dumps({"device_id": "me-uuid"})
    )
    devices = tmp_path / "thread-sync" / "devices"
    devices.mkdir(parents=True)

    # The exchange is not mirrored by Openbase Sync: nothing to check.
    assert hw._thread_exchange_warnings() == []

    sync_daemon.write_config(
        sync_daemon.SyncDaemonConfig(
            device_id="laptop",
            sync_group="default",
            role="edge",
            pair_secret="s",
            roots=[sync_daemon.root_entry(tmp_path / "thread-sync")],
            peer_hot="hub:22100",
            peer_bulk="hub:22101",
        )
    )

    # Nobody has exported anything: both warnings fire.
    ids = [w["id"] for w in hw._thread_exchange_warnings()]
    assert ids == ["thread-sync-no-peer-snapshots", "thread-sync-not-exporting"]

    # Own exports only: peer side dead.
    (devices / "me-uuid").mkdir()
    ids = [w["id"] for w in hw._thread_exchange_warnings()]
    assert ids == ["thread-sync-no-peer-snapshots"]

    # Both sides exporting: clean.
    (devices / "them-uuid").mkdir()
    assert hw._thread_exchange_warnings() == []

    # No peer connected: never warn (an idle machine being off is normal).
    peers.clear()
    (devices / "them-uuid").rmdir()
    assert hw._thread_exchange_warnings() == []


def test_freshness_handshake_is_opt_in_and_passes_loaded_stamp(monkeypatch):
    from types import SimpleNamespace

    from rest_framework.test import APIRequestFactory, force_authenticate

    calls = []
    monkeypatch.setattr(hw, "collect_warnings_cached", lambda: [])
    monkeypatch.setattr(
        "openbase_coder_cli.services.freshness.collector.collect_freshness",
        lambda client: calls.append(client) or {"enabled": True, "components": []},
    )
    factory = APIRequestFactory()
    request = factory.get("/api/health/warnings/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    assert hw.health_warnings(request).data == {"warnings": []}
    assert calls == []
    body = {"component": "desktop", "build": {"schema_version": 1}}
    request = factory.post("/api/health/warnings/", body, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = hw.health_warnings(request)
    assert response.status_code == 200
    assert response.data["freshness"]["enabled"] is True
    assert calls == [body]


def test_codex_version_skew_warning_offers_restart(monkeypatch) -> None:
    from openbase_coder_cli.services import codex_version_skew as skew_module

    monkeypatch.setattr(
        skew_module,
        "collect_codex_version_skews",
        lambda: [
            skew_module.CodexVersionSkew(
                service="codex-app-server",
                running_version="0.155.0",
                installed_version="0.156.1",
                installed_path="/opt/codex",
            )
        ],
    )
    warnings = hw._codex_version_skew_warnings()
    assert [w["id"] for w in warnings] == ["service-restart-needed:codex-app-server"]
    assert warnings[0]["severity"] == "warning"
    assert "0.155.0" in warnings[0]["message"]
    assert "0.156.1" in warnings[0]["message"]

    monkeypatch.setattr(skew_module, "collect_codex_version_skews", lambda: [])
    assert hw._codex_version_skew_warnings() == []

    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(skew_module, "collect_codex_version_skews", boom)
    assert hw._codex_version_skew_warnings() == []


def test_codex_cli_behind_shared_daemon_warns_to_upgrade_without_restart(
    monkeypatch,
) -> None:
    from openbase_coder_cli.services import codex_version_skew as skew_module

    monkeypatch.setattr(
        skew_module,
        "collect_codex_version_skews",
        lambda: [
            skew_module.CodexVersionSkew(
                service="codex-app-server",
                running_version="0.161.0",
                installed_version="0.160.1",
                installed_path="/opt/codex",
                shared_daemon=True,
            ),
            skew_module.CodexVersionSkew(
                service="codex-app-server-dispatcher",
                running_version="0.161.0",
                installed_version="0.160.1",
                installed_path="/opt/codex",
            ),
        ],
    )
    warnings = hw._codex_version_skew_warnings()
    # Neither id carries the restart prefix the banner turns into a button.
    assert [w["id"] for w in warnings] == [
        "codex-cli-outdated:codex-app-server",
        "codex-cli-outdated:codex-app-server-dispatcher",
    ]
    for warning in warnings:
        assert warning["severity"] == "warning"
        assert "upgrade the Codex CLI to 0.161.0" in warning["message"]
        assert "@openai/codex@0.161.0" in warning["action"]
        assert "restart" not in warning["message"].lower()
