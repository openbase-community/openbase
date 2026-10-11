"""Agent configuration sync (``[agents] sync_config``) for Openbase Sync."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from openbase_coder_cli import sync_daemon
from openbase_coder_cli.cli.sync_daemon import sync_daemon_cli


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sync_daemon, "SYNC_DAEMON_CONFIG_PATH", tmp_path / "config.toml"
    )


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


def test_on_by_default_until_turned_off():
    assert sync_daemon.agent_config_enabled() is None
    _configure()
    assert sync_daemon.agent_config_enabled() is True
    assert sync_daemon.read_config_summary()["agent_config"] is True

    sync_daemon.set_agent_config(False)
    assert _load()["agents"] == {"sync_config": False}
    assert sync_daemon.agent_config_enabled() is False
    assert sync_daemon.read_config_summary()["agent_config"] is False

    sync_daemon.set_agent_config(True)
    assert _load()["agents"] == {"sync_config": True}
    assert sync_daemon.agent_config_enabled() is True


def test_set_agent_config_keeps_everything_else():
    _configure(
        '\n[agents]\nsync_config = true\npoll_seconds = 5\n\n[relay]\nexec_allow = ["open"]\n'
    )
    before = _load()
    sync_daemon.set_agent_config(False)
    after = _load()
    assert after["agents"] == {"sync_config": False, "poll_seconds": 5}
    assert after["relay"] == before["relay"]
    assert after["roots"] == before["roots"]
    text = sync_daemon.SYNC_DAEMON_CONFIG_PATH.read_text()
    assert text.count("[agents]") == 1


def test_set_judgment_still_edits_its_own_table_only():
    _configure("\n[agents]\nsync_config = false\n")
    sync_daemon.set_judgment(True, "desktop-1")
    data = _load()
    assert data["judgment"] == {"enabled": True, "device_id": "desktop-1"}
    assert data["agents"] == {"sync_config": False}


def test_cli_disable_enable_and_status(monkeypatch):
    _configure()
    restarts: list[bool] = []
    monkeypatch.setattr(
        sync_daemon,
        "restart_service_if_installed",
        lambda: restarts.append(True) or True,
    )
    runner = CliRunner()

    result = runner.invoke(sync_daemon_cli, ["agent-config", "disable", "--no-restart"])
    assert result.exit_code == 0, result.output
    assert "off" in result.output and "Not restarting" in result.output
    assert _load()["agents"]["sync_config"] is False
    assert restarts == []

    result = runner.invoke(sync_daemon_cli, ["agent-config", "status"])
    assert result.exit_code == 0 and "off" in result.output

    result = runner.invoke(sync_daemon_cli, ["agent-config", "enable", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["enabled"] is True and payload["restart"]["restarted"] is True
    assert restarts == [True]

    result = runner.invoke(sync_daemon_cli, ["agent-config", "status", "--json"])
    assert json.loads(result.output) == {"configured": True, "enabled": True}
