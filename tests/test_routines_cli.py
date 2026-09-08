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


def test_doctor_reports_stale_routines_and_missing_runner(
    monkeypatch, tmp_path
) -> None:
    import json

    class DoctorClient(FakeRoutinesClient):
        async def list_routines(self) -> dict[str, Any]:
            self.calls.append(("list_routines", {}))
            return {
                "count": 1,
                "routines": [
                    {
                        "name": "daily-report",
                        "enabled": True,
                        "scheduleType": "daily",
                        "lastStatus": "stale",
                        "lastError": "Routine run stuck in started; marked stale.",
                        "lastRunDate": "2020-01-01",
                        "lastStartedAt": "2020-01-01T00:00:00.000Z",
                        "nextRunAt": "2020-01-02T00:00:00.000Z",
                    }
                ],
            }

    FakeRoutinesClient.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", DoctorClient)
    paths = importlib.import_module("openbase_coder_cli.paths")
    monkeypatch.setattr(paths, "DEFAULT_LOG_DIR", tmp_path)

    result = CliRunner().invoke(routines_cli.routines, ["doctor"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["healthy"] is False
    routine = payload["routines"][0]
    assert routine["lastStatus"] == "stale"
    assert any("stale" in warning for warning in routine["warnings"])
    assert any("No run recorded" in warning for warning in routine["warnings"])
    assert "may not be running" in payload["runner"]["warning"]


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


class FakeTriggerClient(FakeRoutinesClient):
    async def add_routine_trigger(
        self, name: str, trigger_input: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("add_routine_trigger", {"name": name, **trigger_input}))
        return {"routine": {"name": name}, "trigger": {"id": "trg-1", "token": "tok"}}

    async def remove_routine_trigger(
        self, name: str, trigger_id: str
    ) -> dict[str, Any]:
        self.calls.append(
            ("remove_routine_trigger", {"name": name, "triggerId": trigger_id})
        )
        return {"deleted": True, "routine": {"name": name}}

    async def emit_routine_event(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "emit_routine_event",
                {"name": name, "payload": payload, "eventId": event_id},
            )
        )
        return {"status": "delivered", "routine": name}


def test_add_webhook_trigger_builds_trigger_input(monkeypatch) -> None:
    import json

    FakeRoutinesClient.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeTriggerClient)

    result = CliRunner().invoke(
        routines_cli.routines,
        [
            "add-webhook-trigger",
            "pr-loop",
            "--description",
            "GitHub PR comments",
            "--sender-path",
            "sender.id",
            "--allow-sender",
            "12345",
            "--filter",
            "comment.body",
            "startsWith",
            "/openbase",
            "--hmac-secret",
            "shhh",
        ],
    )

    assert result.exit_code == 0, result.output
    call = FakeRoutinesClient.instances[0].calls[0]
    assert call == (
        "add_routine_trigger",
        {
            "name": "pr-loop",
            "description": "GitHub PR comments",
            "senderPath": "sender.id",
            "senderAllowlist": ["12345"],
            "filters": [
                {"path": "comment.body", "op": "startsWith", "value": "/openbase"}
            ],
            "hmacSecret": "shhh",
        },
    )
    payload = json.loads(result.output)
    assert payload["ingestPath"] == "/api/hooks/t/tok/"


def test_remove_trigger_and_emit(monkeypatch) -> None:
    FakeRoutinesClient.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeTriggerClient)
    runner = CliRunner()

    removed = runner.invoke(
        routines_cli.routines, ["remove-trigger", "pr-loop", "trg-1"]
    )
    emitted = runner.invoke(
        routines_cli.routines,
        ["emit", "pr-loop", "--data", '{"note": "manual"}', "--event-id", "evt-1"],
    )
    bad = runner.invoke(routines_cli.routines, ["emit", "pr-loop", "--data", "{nope"])

    assert removed.exit_code == 0, removed.output
    assert emitted.exit_code == 0, emitted.output
    assert bad.exit_code != 0
    calls = [call for client in FakeRoutinesClient.instances for call in client.calls]
    assert (
        "remove_routine_trigger",
        {"name": "pr-loop", "triggerId": "trg-1"},
    ) in calls
    assert (
        "emit_routine_event",
        {"name": "pr-loop", "payload": {"note": "manual"}, "eventId": "evt-1"},
    ) in calls


def test_add_webhook_trigger_cloud_flag_creates_relay_endpoint(monkeypatch) -> None:
    from types import SimpleNamespace

    FakeRoutinesClient.instances = []
    monkeypatch.setattr(routines_cli, "CodexAppServerClient", FakeTriggerClient)

    cloud_events = importlib.import_module(
        "openbase_coder_cli.services.cloud_webhook_events"
    )
    created = []

    def fake_create(description=""):
        created.append(description)
        return SimpleNamespace(
            ok=True,
            response={
                "id": "ep-1",
                "url": "https://cloud/api/openbase/hooks/t/obhk_x/",
            },
        )

    monkeypatch.setattr(cloud_events, "create_relay_endpoint", fake_create)

    result = CliRunner().invoke(
        routines_cli.routines,
        ["add-webhook-trigger", "cmd-loop", "--cloud", "--description", "PRs"],
    )

    assert result.exit_code == 0, result.output
    assert created == ["PRs"]
    call = FakeRoutinesClient.instances[0].calls[0]
    assert call[1]["relayEndpointId"] == "ep-1"
    assert call[1]["relayUrl"] == "https://cloud/api/openbase/hooks/t/obhk_x/"
