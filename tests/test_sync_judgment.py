"""AI conflict-label opt-in for Openbase Sync (``[judgment]`` table)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from openbase_coder_cli import sync_daemon
from openbase_coder_cli.cli.sync_daemon import sync_daemon_cli
from openbase_coder_cli.services import cloud_registration
from openbase_coder_cli.services.cloud_registration import CloudReportResult

UNMANAGED_TAIL = """
[relay]
exec_allow = ["open"]
"""


def _configure(extra: str = "") -> Path:
    config = sync_daemon.SyncDaemonConfig(
        device_id="sync-laptop",
        sync_group="default",
        role="edge",
        pair_secret="s",
        roots=[{"id": "projects", "path": "~/Projects", "pins": ["."]}],
        peer_hot="hub:22100",
        peer_bulk="hub:22101",
    )
    path = sync_daemon.write_config(config)
    if extra:
        path.write_text(path.read_text() + extra)
    return path


def _load() -> dict:
    return tomllib.loads(sync_daemon.SYNC_DAEMON_CONFIG_PATH.read_text())


def _without_judgment(data: dict) -> dict:
    return {key: value for key, value in data.items() if key != "judgment"}


# --- config editing -------------------------------------------------------


def test_set_judgment_appends_table_and_keeps_everything_else():
    _configure(UNMANAGED_TAIL)
    before = _load()

    sync_daemon.set_judgment(True, "desktop-abc")

    after = _load()
    assert after["judgment"] == {"enabled": True, "device_id": "desktop-abc"}
    assert _without_judgment(after) == before


def test_set_judgment_updates_in_place_and_keeps_unmanaged_judgment_keys():
    _configure(
        "\n[judgment]\n"
        "# opt-in managed by the CLI\n"
        "enabled = false\n"
        'endpoint = "https://example.test/judge/"\n'
        "token_command = [\n"
        '  "openbase-coder",\n'
        '  "auth",\n'
        "]\n"
        'device_id = "desktop-old"\n' + UNMANAGED_TAIL
    )
    before = _load()

    sync_daemon.set_judgment(True, "desktop-new")

    after = _load()
    assert after["judgment"] == {
        "enabled": True,
        "device_id": "desktop-new",
        "endpoint": "https://example.test/judge/",
        "token_command": ["openbase-coder", "auth"],
    }
    assert _without_judgment(after) == _without_judgment(before)
    text = sync_daemon.SYNC_DAEMON_CONFIG_PATH.read_text()
    assert text.count("[judgment]") == 1
    assert "# opt-in managed by the CLI" in text


def test_set_judgment_disable_keeps_device_id():
    _configure('\n[judgment]\nenabled = true\ndevice_id = "desktop-abc"\n')

    sync_daemon.set_judgment(False)

    assert _load()["judgment"] == {"enabled": False, "device_id": "desktop-abc"}


def test_set_judgment_survives_root_rewrites():
    _configure()
    sync_daemon.set_judgment(True, "desktop-abc")

    sync_daemon.add_roots(["~/Notes"])

    data = _load()
    assert data["judgment"] == {"enabled": True, "device_id": "desktop-abc"}
    assert [root["path"] for root in data["roots"]] == ["~/Projects", "~/Notes"]


def test_set_judgment_requires_a_config():
    with pytest.raises(sync_daemon.SyncDaemonError, match="configure"):
        sync_daemon.set_judgment(True, "desktop-abc")


def test_set_judgment_refuses_layouts_it_cannot_edit_safely():
    path = _configure()
    # A dotted top-level key defines the table without a [judgment] header.
    path.write_text(
        path.read_text().replace(
            "[placement]", "judgment.enabled = false\n\n[placement]"
        )
    )
    original = path.read_text()

    with pytest.raises(sync_daemon.SyncDaemonError, match="by hand"):
        sync_daemon.set_judgment(True, "desktop-abc")

    assert path.read_text() == original


# --- reading ----------------------------------------------------------------


def test_judgment_settings_and_summary():
    assert sync_daemon.judgment_settings() is None
    _configure()
    assert sync_daemon.judgment_settings() is None
    summary = sync_daemon.read_config_summary()
    assert summary["judgment_enabled"] is False
    assert "judgment_device_id" not in summary

    sync_daemon.set_judgment(True, "desktop-abc")

    assert sync_daemon.judgment_settings() == {
        "enabled": True,
        "device_id": "desktop-abc",
    }
    summary = sync_daemon.read_config_summary()
    assert summary["judgment_enabled"] is True
    assert summary["judgment_device_id"] == "desktop-abc"


def test_judgment_device_id_falls_back_to_sync_device_id():
    _configure("\n[judgment]\nenabled = true\n")

    assert sync_daemon.judgment_settings() == {
        "enabled": True,
        "device_id": "sync-laptop",
    }


# --- registration payload ---------------------------------------------------


@pytest.fixture
def payload_env(monkeypatch):
    monkeypatch.setattr(
        cloud_registration,
        "tailscale_self_identity",
        lambda: {"available": False, "ips": []},
    )
    monkeypatch.setattr(cloud_registration, "_device_id", lambda: "desktop-1")


def test_payload_omits_judgment_without_table(payload_env):
    assert "judgment_enabled" not in cloud_registration.device_registration_payload()
    _configure()
    assert "judgment_enabled" not in cloud_registration.device_registration_payload()


@pytest.mark.parametrize("enabled", [True, False])
def test_payload_carries_judgment_opt_in(payload_env, enabled):
    _configure()
    sync_daemon.set_judgment(enabled, "desktop-1")

    payload = cloud_registration.device_registration_payload()

    assert payload["judgment_enabled"] is enabled
    assert "judgment_enabled" not in payload["capabilities"]


def test_payload_survives_unreadable_config(payload_env):
    path = sync_daemon.SYNC_DAEMON_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[judgment\n")

    assert "judgment_enabled" not in cloud_registration.device_registration_payload()


# --- commands ---------------------------------------------------------------


@pytest.fixture
def command_env(monkeypatch, payload_env):
    calls: dict[str, list] = {"register": [], "restart": []}

    def fake_register_and_report(**kwargs):
        if calls.get("register_exception"):
            raise RuntimeError(str(calls["register_exception"]))
        calls["register"].append(cloud_registration.device_registration_payload())
        return calls.get("register_result") or CloudReportResult(
            ok=True, supported=True
        )

    def fake_restart():
        calls["restart"].append(True)
        return calls.get("installed", True)

    monkeypatch.setattr(
        cloud_registration, "register_and_report", fake_register_and_report
    )
    monkeypatch.setattr(cloud_registration, "local_device_id", lambda: "desktop-1")
    monkeypatch.setattr(sync_daemon, "restart_service_if_installed", fake_restart)
    return calls


def test_enable_writes_config_registers_and_restarts(command_env):
    _configure(UNMANAGED_TAIL)
    before = _load()

    result = CliRunner().invoke(sync_daemon_cli, ["judgment", "enable"])

    assert result.exit_code == 0, result.output
    after = _load()
    assert after["judgment"] == {"enabled": True, "device_id": "desktop-1"}
    assert _without_judgment(after) == before
    assert [p["judgment_enabled"] for p in command_env["register"]] == [True]
    assert command_env["restart"] == [True]
    assert "Restarted the sync-daemon service." in result.output


def test_disable_registers_false_and_restarts(command_env):
    _configure()
    runner = CliRunner()
    assert runner.invoke(sync_daemon_cli, ["judgment", "enable"]).exit_code == 0

    result = runner.invoke(sync_daemon_cli, ["judgment", "disable"])

    assert result.exit_code == 0, result.output
    assert _load()["judgment"] == {"enabled": False, "device_id": "desktop-1"}
    assert [p["judgment_enabled"] for p in command_env["register"]] == [True, False]
    assert command_env["restart"] == [True, True]


def test_disable_writes_cloud_device_id_before_prior_enable(command_env):
    _configure()

    result = CliRunner().invoke(sync_daemon_cli, ["judgment", "disable"])

    assert result.exit_code == 0, result.output
    assert _load()["judgment"] == {"enabled": False, "device_id": "desktop-1"}
    assert [p["judgment_enabled"] for p in command_env["register"]] == [False]


def test_enable_warns_but_succeeds_when_cloud_unreachable(command_env):
    _configure()
    command_env["register_result"] = CloudReportResult(
        ok=False,
        supported=True,
        error="Login required. Run 'openbase-coder login' first.",
    )

    result = CliRunner().invoke(sync_daemon_cli, ["judgment", "enable"])

    assert result.exit_code == 0, result.output
    assert "Warning: could not update Openbase Cloud" in result.output
    assert "Login required" in result.output
    assert _load()["judgment"]["enabled"] is True
    assert command_env["restart"] == [True]


def test_enable_warns_but_succeeds_when_registration_raises(command_env):
    _configure()
    command_env["register_exception"] = "registration exploded"

    result = CliRunner().invoke(sync_daemon_cli, ["judgment", "enable"])

    assert result.exit_code == 0, result.output
    assert "Warning: could not update Openbase Cloud" in result.output
    assert "registration exploded" in result.output
    assert _load()["judgment"]["enabled"] is True
    assert command_env["restart"] == [True]


def test_enable_reports_when_service_not_installed(command_env):
    _configure()
    command_env["installed"] = False

    result = CliRunner().invoke(sync_daemon_cli, ["judgment", "enable"])

    assert result.exit_code == 0, result.output
    assert "not installed" in result.output


def test_enable_no_restart(command_env):
    _configure()

    result = CliRunner().invoke(sync_daemon_cli, ["judgment", "enable", "--no-restart"])

    assert result.exit_code == 0, result.output
    assert command_env["restart"] == []
    assert len(command_env["register"]) == 1


def test_enable_json_output(command_env):
    _configure()
    command_env["installed"] = False

    result = CliRunner().invoke(
        sync_daemon_cli, ["judgment", "enable", "--json", "--no-restart"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["enabled"] is True
    assert payload["device_id"] == "desktop-1"
    assert payload["cloud"]["ok"] is True
    assert payload["restart"] == {"requested": False, "restarted": None}


def test_enable_requires_configured_sync(command_env):
    result = CliRunner().invoke(sync_daemon_cli, ["judgment", "enable"])

    assert result.exit_code != 0
    assert "sync-daemon configure" in result.output
    assert command_env["register"] == [] and command_env["restart"] == []


def test_status_text_and_json(command_env):
    runner = CliRunner()
    result = runner.invoke(sync_daemon_cli, ["judgment", "status", "--json"])
    assert json.loads(result.output) == {
        "configured": False,
        "enabled": False,
        "device_id": None,
    }
    assert "not set up" in runner.invoke(sync_daemon_cli, ["judgment", "status"]).output

    _configure()
    result = runner.invoke(sync_daemon_cli, ["judgment", "status"])
    assert "AI conflict labels: disabled" in result.output

    runner.invoke(sync_daemon_cli, ["judgment", "enable", "--no-restart"])
    result = runner.invoke(sync_daemon_cli, ["judgment", "status"])
    assert result.exit_code == 0, result.output
    assert "AI conflict labels: enabled" in result.output
    assert "desktop-1" in result.output
    result = runner.invoke(sync_daemon_cli, ["judgment", "status", "--json"])
    assert json.loads(result.output) == {
        "configured": True,
        "enabled": True,
        "device_id": "desktop-1",
    }
