"""``openbase-coder sync status|conflicts|resolve`` delegate to the daemon."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from openbase_coder_cli import sync_daemon
from openbase_coder_cli.cli.sync import sync


class FakeClient:
    calls: list[tuple] = []
    fail = False

    def __init__(self, *args, **kwargs):
        pass

    def _maybe_fail(self):
        if FakeClient.fail:
            raise sync_daemon.SyncDaemonError("sync daemon unreachable")

    def status(self):
        self._maybe_fail()
        FakeClient.calls.append(("status",))
        return {
            "device": "laptop",
            "role": "edge",
            "roots": [
                {
                    "id": "projects",
                    "path": "~/Projects",
                    "entries": 1200,
                    "pending_fetches": 3,
                    "scanning": False,
                }
            ],
            "peers": [{"device": "mini", "role": "hub", "rtt_ms": 4.2}],
            "open_conflicts": 1,
        }

    def conflicts(self, root=None):
        self._maybe_fail()
        FakeClient.calls.append(("conflicts", root))
        return [
            {
                "id": 7,
                "root": "projects",
                "path": "app/main.py",
                "kind": "content",
                "a_device": "mini",
                "b_device": "laptop",
                "created_ns": 1_760_000_000_000_000_000,
            },
            {
                "id": 8,
                "root": "projects",
                "path": "app:refs/heads/main",
                "kind": "git-branch",
                "created_ns": 1_760_000_000_000_000_000,
            },
        ]

    def resolve(self, conflict_id, choice):
        self._maybe_fail()
        FakeClient.calls.append(("resolve", conflict_id, choice))


@pytest.fixture
def client(monkeypatch):
    FakeClient.calls = []
    FakeClient.fail = False
    monkeypatch.setattr(sync_daemon, "SyncDaemonClient", FakeClient)
    monkeypatch.setattr(sync_daemon, "is_configured", lambda config_path=None: True)
    monkeypatch.setattr(
        sync_daemon, "read_config_summary", lambda config_path=None: {"device_id": "laptop"}
    )
    return FakeClient


def test_status_summarizes_daemon_status(client):
    result = CliRunner().invoke(sync, ["status"])

    assert result.exit_code == 0, result.output
    assert "Role:      edge" in result.output
    assert "~/Projects  (1200 entries, 3 transferring)" in result.output
    assert "mini (hub, 4 ms)" in result.output
    assert "Conflicts: 1" in result.output
    assert client.calls == [("status",)]


def test_status_json_is_the_raw_payload(client):
    result = CliRunner().invoke(sync, ["status", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["open_conflicts"] == 1


def test_status_unreachable_points_at_the_service(client):
    client.fail = True

    result = CliRunner().invoke(sync, ["status"])

    assert result.exit_code != 0
    assert "services start sync-daemon" in result.output


def test_unconfigured_commands_explain_setup(monkeypatch):
    monkeypatch.setattr(sync_daemon, "is_configured", lambda config_path=None: False)

    for args in (["status"], ["conflicts"], ["resolve", "1", "--keep-local"]):
        result = CliRunner().invoke(sync, args)
        assert result.exit_code != 0
        assert "sync-daemon configure" in result.output


def test_conflicts_lists_records(client):
    result = CliRunner().invoke(sync, ["conflicts"])

    assert result.exit_code == 0, result.output
    assert "7  content       projects:app/main.py  (2025-10-09T" in result.output
    assert client.calls == [("conflicts", None)]

    result = CliRunner().invoke(sync, ["conflicts", "--json"])
    assert json.loads(result.output)[0]["id"] == 7


@pytest.mark.parametrize(
    ("flag", "choice"), [("--keep-local", "b"), ("--use-remote", "a")]
)
def test_resolve_maps_actions_to_daemon_choices(client, flag, choice):
    result = CliRunner().invoke(sync, ["resolve", "7", flag])

    assert result.exit_code == 0, result.output
    assert client.calls == [("conflicts", None), ("resolve", 7, choice)]


def test_resolve_refuses_diverged_branches(client):
    result = CliRunner().invoke(sync, ["resolve", "8", "--keep-local"])

    assert result.exit_code != 0
    assert "neither" in result.output
    assert ("resolve", 8, "a") not in client.calls


def test_resolve_requires_an_action(client):
    result = CliRunner().invoke(sync, ["resolve", "7"])

    assert result.exit_code != 0
    assert "--keep-local or --use-remote" in result.output
    assert client.calls == []


def test_removed_syncthing_commands_are_gone():
    for name in ("enable", "disable", "add", "remove", "ignores", "reconcile"):
        assert name not in sync.commands
