from __future__ import annotations

import importlib
import threading
from typing import Any

from click.testing import CliRunner

routines_cli = importlib.import_module("openbase_coder_cli.cli.routines")


class FakeExecutionClient:
    instances: list["FakeExecutionClient"] = []

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        FakeExecutionClient.instances.append(self)

    async def close(self) -> None:
        self.calls.append(("close", {}))


class FakeRoutineManager:
    instances: list["FakeRoutineManager"] = []

    def __init__(self, client: FakeExecutionClient) -> None:
        self.client = client
        self.calls: list[tuple[str, Any]] = []
        FakeRoutineManager.instances.append(self)

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


def test_background_tick_does_not_start_duplicate_work() -> None:
    started = threading.Event()
    release = threading.Event()

    def target() -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=1)
        return {"repos": []}

    tick = routines_cli._BackgroundTick(target)

    assert tick.start() is True
    assert started.wait(timeout=1)
    assert tick.start() is False
    assert tick.poll() is None

    release.set()
    result = None
    for _ in range(100):
        result = tick.poll()
        if result is not None:
            break
        threading.Event().wait(0.01)
    assert result == ({"repos": []}, None)


def test_create_routine_calls_openbase_manager(monkeypatch) -> None:
    FakeExecutionClient.instances = []
    FakeRoutineManager.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeExecutionClient)
    monkeypatch.setattr(routines_cli, "RoutineManager", FakeRoutineManager)

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
    manager = FakeRoutineManager.instances[0]
    assert manager.calls[0] == (
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
    assert FakeExecutionClient.instances[0].calls[-1] == ("close", {})


def test_create_routine_can_request_fresh_thread_per_run(monkeypatch) -> None:
    FakeRoutineManager.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeExecutionClient)
    monkeypatch.setattr(routines_cli, "RoutineManager", FakeRoutineManager)

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
    assert FakeRoutineManager.instances[0].calls[0][1]["freshThreadPerRun"] is True


def test_create_interval_routine_calls_openbase_manager(monkeypatch) -> None:
    FakeRoutineManager.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeExecutionClient)
    monkeypatch.setattr(routines_cli, "RoutineManager", FakeRoutineManager)

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
    manager = FakeRoutineManager.instances[0]
    assert manager.calls[0] == (
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


def test_create_command_routine_calls_openbase_manager(monkeypatch) -> None:
    FakeRoutineManager.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeExecutionClient)
    monkeypatch.setattr(routines_cli, "RoutineManager", FakeRoutineManager)

    result = CliRunner().invoke(
        routines_cli.routines,
        [
            "create",
            "discover-prs",
            "--kind",
            "command",
            "--command",
            "openbase-coder-open-pr-review-discover --workspace .",
            "--command-timeout-seconds",
            "120",
            "--interval-seconds",
            "60",
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeRoutineManager.instances[0].calls[0] == (
        "save_routine",
        {
            "name": "discover-prs",
            "prompt": "",
            "kind": "command",
            "command": "openbase-coder-open-pr-review-discover --workspace .",
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
    FakeRoutineManager.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeExecutionClient)
    monkeypatch.setattr(routines_cli, "RoutineManager", FakeRoutineManager)

    result = CliRunner().invoke(
        routines_cli.routines,
        ["run-due", "--name", "daily", "--force"],
    )

    assert result.exit_code == 0, result.output
    assert FakeRoutineManager.instances[0].calls[0] == (
        "run_due_routines",
        {"name": "daily", "force": True},
    )


def test_update_routine_can_toggle_fresh_thread_per_run(monkeypatch) -> None:
    FakeRoutineManager.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeExecutionClient)
    monkeypatch.setattr(routines_cli, "RoutineManager", FakeRoutineManager)

    result = CliRunner().invoke(
        routines_cli.routines,
        ["update", "daily", "--reuse-target-thread"],
    )

    assert result.exit_code == 0, result.output
    assert FakeRoutineManager.instances[0].calls[0] == (
        "save_routine",
        {"name": "daily", "freshThreadPerRun": False},
    )


def test_update_routine_can_switch_to_command(monkeypatch) -> None:
    FakeRoutineManager.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeExecutionClient)
    monkeypatch.setattr(routines_cli, "RoutineManager", FakeRoutineManager)

    result = CliRunner().invoke(
        routines_cli.routines,
        [
            "update",
            "open-pr-review-routine",
            "--kind",
            "command",
            "--command",
            "discover",
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeRoutineManager.instances[0].calls[0] == (
        "save_routine",
        {"name": "open-pr-review-routine", "kind": "command", "command": "discover"},
    )
