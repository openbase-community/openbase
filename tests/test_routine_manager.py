from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openbase_coder_cli.routines.manager import RoutineManager
from openbase_coder_cli.routines.store import RoutineStateError, RoutineStore


class FakeExecutionClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.session: Any | None = None
        self.thread_payload: dict[str, Any] = {"thread": {"turns": []}}

    async def start_thread(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("start_thread", input_data))
        return {"threadId": "thread-new"}

    async def start_turn(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("start_turn", input_data))
        return {"threadId": input_data["threadId"], "turnId": "turn-new"}

    async def read_thread(
        self, thread_id: str, include_turns: bool = True
    ) -> dict[str, Any]:
        self.calls.append(
            ("read_thread", {"thread_id": thread_id, "include_turns": include_turns})
        )
        return self.thread_payload

    async def resolve_queue_target(self, input_data: Any) -> Any:
        self.calls.append(("resolve_queue_target", input_data))
        return SimpleNamespace(
            session=SimpleNamespace(thread_id=input_data.thread_id, agent_name="Ada")
        )

    async def start_or_queue_turn(
        self, target: Any, turn_input: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("start_or_queue_turn", turn_input))
        return {"threadId": target.session.thread_id, "turnId": "turn-existing"}

    async def get_session(self, thread_id: str) -> Any | None:
        self.calls.append(("get_session", thread_id))
        return self.session


def routine_store(tmp_path: Path) -> RoutineStore:
    return RoutineStore(
        tmp_path / "openbase" / "routines.json",
        legacy_state_path=tmp_path / "super-agents" / "state.json",
    )


def test_store_migrates_legacy_routines_without_touching_sessions(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "super-agents" / "state.json"
    legacy_path.parent.mkdir()
    legacy_path.write_text(
        json.dumps(
            {
                "sessions": {"thread-1": {"threadId": "thread-1"}},
                "futureField": {"preserve": True},
                "routines": {
                    "daily": {
                        "name": "daily",
                        "prompt": "Check status.",
                        "time": "09:00",
                        "updatedAt": "2026-07-01T00:00:00.000Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = routine_store(tmp_path)
    state = store.read()

    assert list(state.routines) == ["daily"]
    migrated = json.loads(store.path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 1
    assert migrated["routines"]["daily"]["prompt"] == "Check status."
    remaining = json.loads(legacy_path.read_text(encoding="utf-8"))
    assert "routines" not in remaining
    assert remaining["sessions"] == {"thread-1": {"threadId": "thread-1"}}
    assert remaining["futureField"] == {"preserve": True}


def test_store_does_not_remigrate_after_new_state_exists(tmp_path: Path) -> None:
    store = routine_store(tmp_path)
    store.path.parent.mkdir()
    store.path.write_text('{"schema_version": 1, "routines": {}}', encoding="utf-8")
    store.legacy_state_path.parent.mkdir()
    store.legacy_state_path.write_text(
        '{"routines": {"stale": {"name": "stale", "prompt": "old", "time": "09:00"}}}',
        encoding="utf-8",
    )

    assert store.read().routines == {}
    assert "routines" in json.loads(store.legacy_state_path.read_text(encoding="utf-8"))


def test_store_refuses_newer_schema(tmp_path: Path) -> None:
    store = routine_store(tmp_path)
    store.path.parent.mkdir()
    store.path.write_text('{"schema_version": 2, "routines": {}}', encoding="utf-8")

    with pytest.raises(RoutineStateError, match="newer Openbase Coder"):
        store.read()


@pytest.mark.asyncio
async def test_agent_routine_uses_generic_turn_primitives(tmp_path: Path) -> None:
    client = FakeExecutionClient()
    manager = RoutineManager(client, store=routine_store(tmp_path))
    await manager.save_routine(
        {
            "name": "daily",
            "prompt": "Check status.",
            "time": "00:00",
            "timezone": "UTC",
            "threadId": "thread-existing",
        }
    )

    result = await manager.run_due_routines()

    assert result["count"] == 1
    assert result["results"][0]["ran"] is True
    assert result["results"][0]["turnId"] == "turn-existing"
    assert [name for name, _value in client.calls].count("start_or_queue_turn") == 1
    stored = await manager.read_routine("daily")
    assert stored["routine"]["lastStatus"] == "started"


@pytest.mark.asyncio
async def test_command_failure_is_visible_in_routine_state(tmp_path: Path) -> None:
    manager = RoutineManager(FakeExecutionClient(), store=routine_store(tmp_path))
    await manager.save_routine(
        {
            "name": "failing-command",
            "kind": "command",
            "command": "exit 7",
            "time": "00:00",
            "timezone": "UTC",
        }
    )

    result = await manager.run_due_routines(name="failing-command", force=True)

    assert result["results"][0]["exitCode"] == 7
    assert result["results"][0]["ran"] is False
    assert result["failureCount"] == 1
    stored = await manager.read_routine("failing-command")
    assert stored["routine"]["lastStatus"] == "failed"
    assert stored["routine"]["lastError"] == "Command routine exited with code 7."


@pytest.mark.asyncio
async def test_reservation_prevents_duplicate_due_runs(tmp_path: Path) -> None:
    store = routine_store(tmp_path)
    first = RoutineManager(FakeExecutionClient(), store=store)
    second = RoutineManager(FakeExecutionClient(), store=routine_store(tmp_path))
    await first.save_routine(
        {
            "name": "daily",
            "prompt": "Check status.",
            "time": "00:00",
            "timezone": "UTC",
            "threadId": "thread-existing",
        }
    )

    assert [item.name for item in await first.reserve_due_routines()] == ["daily"]
    assert (await second.run_due_routines())["count"] == 0
    assert (await second.read_routine("daily"))["routine"]["lastStatus"] == "starting"
    assert store.path.with_suffix(".json.lock").exists()


@pytest.mark.asyncio
async def test_terminal_turn_status_is_reconciled(tmp_path: Path) -> None:
    client = FakeExecutionClient()
    client.session = SimpleNamespace(
        turns={
            "turn-1": SimpleNamespace(
                status="cancelled", updated_at="2026-07-14T12:00:00.000Z"
            )
        },
        last_turn_id="turn-1",
        last_status="cancelled",
        last_event_at="2026-07-14T12:00:00.000Z",
        updated_at="2026-07-14T12:00:00.000Z",
    )
    manager = RoutineManager(client, store=routine_store(tmp_path))
    await manager.save_routine(
        {
            "name": "daily",
            "prompt": "Check status.",
            "time": "09:00",
            "lastThreadId": "thread-1",
            "lastTurnId": "turn-1",
            "lastStatus": "started",
        }
    )

    stored = await manager.read_routine("daily")

    assert stored["routine"]["lastStatus"] == "failed"
    assert stored["routine"]["lastError"] == "Routine turn became cancelled."


@pytest.mark.asyncio
async def test_authoritative_thread_read_reconciles_stale_session_cache(
    tmp_path: Path,
) -> None:
    client = FakeExecutionClient()
    client.thread_payload = {
        "thread": {
            "id": "thread-1",
            "updatedAt": 1_783_987_200,
            "turns": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "completedAt": 1_783_987_200,
                }
            ],
        }
    }
    client.session = SimpleNamespace(
        turns={"turn-1": SimpleNamespace(status="running", updated_at=None)},
        last_turn_id="turn-1",
        last_status="running",
        last_event_at=None,
        updated_at=None,
    )
    manager = RoutineManager(client, store=routine_store(tmp_path))
    await manager.save_routine(
        {
            "name": "daily",
            "prompt": "Check status.",
            "time": "09:00",
            "lastThreadId": "thread-1",
            "lastTurnId": "turn-1",
            "lastStatus": "started",
        }
    )

    stored = await manager.read_routine("daily")

    assert stored["routine"]["lastStatus"] == "completed"
    assert not any(name == "get_session" for name, _value in client.calls)
