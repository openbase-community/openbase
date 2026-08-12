from __future__ import annotations

import importlib
from typing import Any

from click.testing import CliRunner

routines_cli = importlib.import_module("openbase_coder_cli.cli.routines")


class FakeRoutinesClient:
    instances: list["FakeRoutinesClient"] = []

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        FakeRoutinesClient.instances.append(self)

    async def close(self) -> None:
        self.calls.append(("close", {}))

    async def save_routine(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("save_routine", input_data))
        return {"routine": input_data}

    async def list_routines(self) -> dict[str, Any]:
        self.calls.append(("list_routines", {}))
        return {"count": 0, "routines": []}

    async def run_due_routines(
        self,
        name: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(("run_due_routines", {"name": name, "force": force}))
        return {"count": 1, "results": [{"name": name or "daily"}]}


def test_create_routine_calls_super_agents_library(monkeypatch) -> None:
    FakeRoutinesClient.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeRoutinesClient)

    result = CliRunner().invoke(
        routines_cli.routines,
        [
            "create",
            "daily",
            "--prompt",
            "Check status",
            "--time",
            "9:05",
            "--timezone",
            "UTC",
            "--thread-id",
            "thread-1",
        ],
    )

    assert result.exit_code == 0, result.output
    client = FakeRoutinesClient.instances[0]
    assert client.calls[0] == (
        "save_routine",
        {
            "name": "daily",
            "kind": "agent",
            "prompt": "Check status",
            "time": "09:05",
            "scheduleType": "daily",
            "timezone": "UTC",
            "enabled": True,
            "threadId": "thread-1",
            "approvalPolicy": "never",
            "sandboxType": "dangerFullAccess",
            "mode": "default",
        },
    )
    assert client.calls[-1] == ("close", {})


def test_create_routine_can_request_fresh_thread_per_run(monkeypatch) -> None:
    FakeRoutinesClient.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeRoutinesClient)

    result = CliRunner().invoke(
        routines_cli.routines,
        [
            "create",
            "daily",
            "--prompt",
            "Check status",
            "--time",
            "09:05",
            "--fresh-thread-per-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeRoutinesClient.instances[0].calls[0][1]["freshThreadPerRun"] is True


def test_create_interval_routine_calls_super_agents_library(monkeypatch) -> None:
    FakeRoutinesClient.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeRoutinesClient)

    result = CliRunner().invoke(
        routines_cli.routines,
        [
            "create",
            "poll-prioritized",
            "--prompt",
            "Check Notion priority.",
            "--interval-seconds",
            "60",
            "--thread-id",
            "thread-1",
        ],
    )

    assert result.exit_code == 0, result.output
    client = FakeRoutinesClient.instances[0]
    assert client.calls[0] == (
        "save_routine",
        {
            "name": "poll-prioritized",
            "kind": "agent",
            "prompt": "Check Notion priority.",
            "scheduleType": "interval",
            "intervalSeconds": 60,
            "timezone": "America/New_York",
            "enabled": True,
            "threadId": "thread-1",
            "approvalPolicy": "never",
            "sandboxType": "dangerFullAccess",
            "mode": "default",
        },
    )


def test_create_command_routine_calls_super_agents_library(monkeypatch) -> None:
    FakeRoutinesClient.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeRoutinesClient)

    result = CliRunner().invoke(
        routines_cli.routines,
        [
            "create",
            "discover-prs",
            "--kind",
            "command",
            "--command",
            "generate-workspace-report --workspace .",
            "--command-timeout-seconds",
            "120",
            "--interval-seconds",
            "60",
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeRoutinesClient.instances[0].calls[0] == (
        "save_routine",
        {
            "name": "discover-prs",
            "prompt": "",
            "kind": "command",
            "command": "generate-workspace-report --workspace .",
            "commandTimeoutSeconds": 120,
            "scheduleType": "interval",
            "intervalSeconds": 60,
            "timezone": "America/New_York",
            "enabled": True,
            "approvalPolicy": "never",
            "sandboxType": "dangerFullAccess",
            "mode": "default",
        },
    )


def test_run_due_routines_command_supports_force(monkeypatch) -> None:
    FakeRoutinesClient.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeRoutinesClient)

    result = CliRunner().invoke(
        routines_cli.routines,
        ["run-due", "--name", "daily", "--force"],
    )

    assert result.exit_code == 0, result.output
    assert FakeRoutinesClient.instances[0].calls[0] == (
        "run_due_routines",
        {"name": "daily", "force": True},
    )


def test_update_routine_can_toggle_fresh_thread_per_run(monkeypatch) -> None:
    FakeRoutinesClient.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeRoutinesClient)

    result = CliRunner().invoke(
        routines_cli.routines,
        ["update", "daily", "--reuse-target-thread"],
    )

    assert result.exit_code == 0, result.output
    assert FakeRoutinesClient.instances[0].calls[0] == (
        "save_routine",
        {"name": "daily", "freshThreadPerRun": False},
    )


def test_update_routine_can_switch_to_command(monkeypatch) -> None:
    FakeRoutinesClient.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeRoutinesClient)

    result = CliRunner().invoke(
        routines_cli.routines,
        [
            "update",
            "report-routine",
            "--kind",
            "command",
            "--command",
            "discover",
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeRoutinesClient.instances[0].calls[0] == (
        "save_routine",
        {"name": "report-routine", "kind": "command", "command": "discover"},
    )
