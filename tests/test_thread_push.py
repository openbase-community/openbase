"""Push-to-durable orchestration with peers, sync and stores mocked."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from openbase_coder_cli import livekit_voice_route
from openbase_coder_cli.services import durable_targets, thread_push
from openbase_coder_cli.services.durable_targets import DurableTarget, TargetError
from openbase_coder_cli.services.thread_push import PushError
from openbase_coder_cli.thread_sync import thread_handoff, thread_moves
from openbase_coder_cli.thread_sync.models import ThreadInfo, TurnInfo
from openbase_coder_cli.thread_sync.thread_handoff import HandoffError, HandoffSnapshot

HUB = DurableTarget(
    key="mini.tail.ts.net",
    name="mini",
    host="mini.tail.ts.net",
    base_url="http://mini.tail.ts.net:18080",
)
INFO = {
    "accepts_pushes": True,
    "backends": ["codex", "claude_code"],
    "exchange_synced": True,
    "device_name": "mini",
}


class FakeClient:
    backend = "codex"

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        return {"status": "unsubscribed"}


class FakeManager:
    _execution_backend = "codex"

    def __init__(self, threads: dict[str, ThreadInfo]) -> None:
        self.threads = threads
        self._client = FakeClient()
        self.turns: list[tuple[str, str]] = []
        self.approvals: list[dict] = []

    async def get_thread_state(self, thread_id: str) -> ThreadInfo | None:
        return self.threads.get(thread_id)

    async def list_approval_requests(self) -> list[dict]:
        return self.approvals

    async def start_turn(self, thread_id: str, prompt: str, model=None) -> str:
        thread_moves.ensure_thread_writable(thread_id)
        self.turns.append((thread_id, prompt))
        return "turn-1"


def _thread(thread_id: str = "t-1", **fields: Any) -> ThreadInfo:
    return ThreadInfo(
        session_id=thread_id,
        directory=str(Path.home() / "Projects" / "app"),
        backend=fields.pop("backend", "codex"),
        **fields,
    )


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path / "data"))
    route = livekit_voice_route.VoiceRouteState(
        dispatcher_thread_id="dispatcher",
        dispatcher_voice_id="v",
        dispatcher_voice_name="v",
        active_target_thread_id=None,
        active_target_kind=None,
        active_target_label=None,
        active_target_voice_id=None,
        active_target_voice_name=None,
        updated_at=None,
    )
    monkeypatch.setattr(
        livekit_voice_route, "get_livekit_voice_route_state", lambda: route
    )
    calls: dict[str, Any] = {"flush": [], "arrivals": [], "exports": 0}
    monkeypatch.setattr(thread_push, "_synced_root", lambda d: Path(d).parent)
    monkeypatch.setattr(thread_push, "_exchange_synced", lambda: True)
    monkeypatch.setattr(thread_push, "EXPORT_RETRY_SECONDS", 0)
    monkeypatch.setattr(durable_targets, "durable_targets", lambda: [HUB])
    monkeypatch.setattr(durable_targets, "target_info", lambda target: dict(INFO))
    monkeypatch.setattr(durable_targets, "this_computer_is_durable", lambda: False)

    def export_one(backend: str, entity_id: str) -> HandoffSnapshot:
        calls["exports"] += 1
        return HandoffSnapshot(backend, entity_id, "fp-1", "laptop-device")

    monkeypatch.setattr(thread_handoff, "export_one", export_one)

    def barrier(kind: str, path: str, timeout: float) -> dict:
        calls["flush"].append((kind, path))
        return {"result": "ok", "lag": 0}

    monkeypatch.setattr(thread_push, "_barrier", barrier)

    def send_arrival(target: DurableTarget, payload: dict) -> dict:
        calls["arrivals"].append(payload)
        return {
            "state": "ready",
            "thread_id": payload["thread_id"],
            "turn_started": bool(payload.get("message")),
        }

    monkeypatch.setattr(durable_targets, "send_arrival", send_arrival)
    return calls


def _push(manager: FakeManager, thread_id: str = "t-1", **kwargs: Any) -> dict:
    return asyncio.run(thread_push.push_thread(manager, thread_id, **kwargs))


def test_push_moves_the_thread_and_locks_the_local_copy(isolated) -> None:
    manager = FakeManager({"t-1": _thread()})

    result = _push(manager, message="keep going")

    assert result["state"] == "moved"
    assert result["moved_to"]["device"] == "mini"
    assert result["moved_to"]["host"] == "mini.tail.ts.net"
    assert result["turn_started"] is True
    arrival = isolated["arrivals"][0]
    assert arrival["directory"] == "~/Projects/app"
    assert arrival["fingerprint"] == "fp-1"
    assert arrival["message"] == "keep going"
    # The local Codex app-server let go of the thread before export.
    assert manager._client.requests == [("thread/unsubscribe", {"threadId": "t-1"})]
    # Both the project folder and the exchange were flushed to the hub.
    flushed = [path for kind, path in isolated["flush"] if kind == "flush"]
    assert flushed[0].endswith("/Projects/app")
    assert flushed[1].endswith("thread-sync")
    with pytest.raises(thread_moves.ThreadMovedError, match="moved to mini"):
        asyncio.run(manager.start_turn("t-1", "hello"))


def test_push_is_idempotent_once_moved(isolated) -> None:
    manager = FakeManager({"t-1": _thread()})
    first = _push(manager)

    second = _push(manager)

    assert second["operation_id"] == first["operation_id"]
    assert len(isolated["arrivals"]) == 1


def test_running_turn_is_refused_without_side_effects(isolated) -> None:
    running = TurnInfo(run_id="r", started_at=datetime.now())
    manager = FakeManager({"t-1": _thread(current_run=running)})

    with pytest.raises(PushError) as caught:
        _push(manager)

    assert caught.value.code == "thread_busy"
    assert caught.value.safe_to_retry
    assert thread_moves.get_move("t-1") is None
    assert isolated["exports"] == 0


def test_pending_approval_and_queue_and_voice_block_the_push(isolated) -> None:
    manager = FakeManager({"t-1": _thread(), "dispatcher": _thread("dispatcher")})
    manager.approvals = [{"thread_id": "t-1"}]
    with pytest.raises(PushError, match="approval"):
        _push(manager)
    manager.approvals = []
    with pytest.raises(PushError, match="dispatcher"):
        _push(manager, "dispatcher")


def test_folder_outside_synced_roots_is_refused(isolated, monkeypatch) -> None:
    monkeypatch.setattr(thread_push, "_synced_root", lambda d: None)

    with pytest.raises(PushError) as caught:
        _push(FakeManager({"t-1": _thread()}))

    assert caught.value.code == "folder_not_synced"


def test_no_durable_machine_is_explained(isolated, monkeypatch) -> None:
    monkeypatch.setattr(durable_targets, "durable_targets", lambda: [])
    with pytest.raises(PushError, match="No durable machine"):
        _push(FakeManager({"t-1": _thread()}))
    monkeypatch.setattr(durable_targets, "this_computer_is_durable", lambda: True)
    with pytest.raises(PushError, match="is your durable machine"):
        _push(FakeManager({"t-1": _thread()}))


def test_unknown_target_is_refused(isolated) -> None:
    with pytest.raises(PushError) as caught:
        _push(FakeManager({"t-1": _thread()}), to="laptop")
    assert caught.value.code == "unknown_target"


def test_offline_target_leaves_the_thread_usable(isolated, monkeypatch) -> None:
    def offline(target):
        raise TargetError("target_offline", "mini is not reachable", retryable=True)

    monkeypatch.setattr(durable_targets, "target_info", offline)
    manager = FakeManager({"t-1": _thread()})

    with pytest.raises(PushError) as caught:
        _push(manager)

    assert caught.value.code == "target_offline"
    assert caught.value.safe_to_retry
    asyncio.run(manager.start_turn("t-1", "still here"))


def test_missing_backend_on_target_is_refused(isolated, monkeypatch) -> None:
    monkeypatch.setattr(
        durable_targets, "target_info", lambda t: {**INFO, "backends": ["codex"]}
    )
    with pytest.raises(PushError, match="Claude Code is not set up on mini"):
        _push(
            FakeManager({"t-1": _thread(backend="claude_code", backend_session_id="s")})
        )


def test_sync_lag_fails_safely_before_delivery(isolated, monkeypatch) -> None:
    monkeypatch.setattr(
        thread_push,
        "_barrier",
        lambda kind, path, timeout: {"result": "timeout", "lag": 7},
    )
    manager = FakeManager({"t-1": _thread()})

    with pytest.raises(PushError) as caught:
        _push(manager)

    assert caught.value.code == "sync_lagging"
    assert "7 changes pending" in str(caught.value)
    assert thread_moves.get_move("t-1")["state"] == thread_moves.STATE_FAILED
    assert isolated["arrivals"] == []
    asyncio.run(manager.start_turn("t-1", "usable"))


def test_busy_export_is_retried_then_reported(isolated, monkeypatch) -> None:
    attempts = []

    def busy(backend, entity_id):
        attempts.append(1)
        raise HandoffError("thread_busy", "still open", retryable=True)

    monkeypatch.setattr(thread_handoff, "export_one", busy)
    with pytest.raises(PushError) as caught:
        _push(FakeManager({"t-1": _thread()}))
    assert caught.value.code == "thread_busy"
    assert len(attempts) == thread_push.EXPORT_ATTEMPTS


def test_lost_answer_stays_blocked_and_retry_finishes_same_operation(
    isolated, monkeypatch
) -> None:
    def lost(target, payload):
        isolated["arrivals"].append(payload)
        raise TargetError("target_unreachable", "lost", retryable=True, reached=True)

    monkeypatch.setattr(durable_targets, "send_arrival", lost)
    manager = FakeManager({"t-1": _thread()})
    with pytest.raises(PushError):
        _push(manager)
    assert thread_moves.get_move("t-1")["state"] == thread_moves.STATE_UNCERTAIN
    with pytest.raises(thread_moves.ThreadMovedError, match="did not finish"):
        asyncio.run(manager.start_turn("t-1", "nope"))

    def ok(target, payload):
        isolated["arrivals"].append(payload)
        return {"state": "ready", "thread_id": payload["thread_id"]}

    monkeypatch.setattr(durable_targets, "send_arrival", ok)
    result = _push(manager)

    assert result["state"] == "moved"
    first, second = isolated["arrivals"]
    assert first["operation_id"] == second["operation_id"]
    assert isolated["exports"] == 1


def test_concurrent_push_is_rejected(isolated) -> None:
    lock = thread_push._thread_lock("t-1")
    lock.acquire()
    try:
        with pytest.raises(PushError) as caught:
            _push(FakeManager({"t-1": _thread()}))
    finally:
        lock.release()
    assert caught.value.code == "push_in_progress"


def test_cancel_asks_the_target_before_releasing(isolated, monkeypatch) -> None:
    thread_moves.set_move(
        "t-1",
        state=thread_moves.STATE_UNCERTAIN,
        operation_id="00000000-0000-4000-8000-000000000001",
        fingerprint="fp",
        target={**HUB.to_json(), "base_url": HUB.base_url},
    )
    asked = []

    def confirm(record, target):
        asked.append(target.name)
        thread_moves.clear_move(record["thread_id"])
        return {"state": "local", "thread_id": record["thread_id"]}

    monkeypatch.setattr(thread_push, "_confirm_not_arrived", confirm)

    assert asyncio.run(thread_push.cancel_push("t-1"))["state"] == "local"
    assert thread_moves.get_move("t-1") is None
    assert asked == ["mini"]


def test_release_makes_a_moved_copy_usable(isolated) -> None:
    manager = FakeManager({"t-1": _thread()})
    _push(manager)

    thread_push.release_moved("t-1")

    asyncio.run(manager.start_turn("t-1", "back here"))


def test_push_options_report_targets_and_block_reasons(isolated) -> None:
    running = TurnInfo(run_id="r", started_at=datetime.now())
    manager = FakeManager({"t-1": _thread(current_run=running)})

    options = asyncio.run(thread_push.push_options(manager, "t-1"))

    assert "turn is running" in options["blocked_reason"]
    assert options["targets"][0]["online"] is True
    assert options["targets"][0]["reason"] is None


# --- receiving side ---------------------------------------------------------


def _arrival(**overrides: Any) -> dict:
    payload = {
        "operation_id": "00000000-0000-4000-8000-000000000002",
        "thread_id": "t-1",
        "backend": "codex",
        "entity_id": "t-1",
        "fingerprint": "fp-1",
        "source_device_id": "laptop-device",
        "source_device_name": "laptop",
        "directory": "~",
        "message": "continue",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def receiving(monkeypatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"imports": 0}
    monkeypatch.setattr(thread_handoff, "snapshot_present", lambda snapshot: True)
    monkeypatch.setattr(
        thread_handoff, "local_thread_id", lambda snapshot: snapshot.entity_id
    )
    monkeypatch.setattr(thread_push, "_settle_inbound", lambda paths: None)

    def import_one(snapshot):
        calls["imports"] += 1
        return "snapshot_imported"

    monkeypatch.setattr(thread_handoff, "import_one", import_one)
    return calls


def test_accept_imports_once_and_starts_the_follow_up_once(isolated, receiving) -> None:
    manager = FakeManager({"t-1": _thread()})

    first = asyncio.run(thread_push.accept_push(manager, _arrival()))
    second = asyncio.run(thread_push.accept_push(manager, _arrival()))

    assert first == second
    assert first["thread_id"] == "t-1"
    assert first["turn_started"] is True
    assert receiving["imports"] == 1
    assert manager.turns == [("t-1", "continue")]
    assert thread_push.arrival_status(_arrival()["operation_id"])["state"] == "ready"


def test_accept_clears_a_stale_moved_marker(isolated, receiving) -> None:
    thread_moves.set_move(
        "t-1", state=thread_moves.STATE_MOVED, target={"name": "laptop"}
    )
    manager = FakeManager({"t-1": _thread()})

    asyncio.run(thread_push.accept_push(manager, _arrival(message=None)))

    assert thread_moves.get_move("t-1") is None


def test_accept_refuses_missing_backend_and_folder(
    isolated, receiving, monkeypatch
) -> None:
    manager = FakeManager({"t-1": _thread()})
    with pytest.raises(PushError, match="Claude Code is not set up"):
        asyncio.run(thread_push.accept_push(manager, _arrival(backend="claude_code")))
    with pytest.raises(PushError) as caught:
        asyncio.run(
            thread_push.accept_push(
                manager, _arrival(directory="~/definitely-missing-x")
            )
        )
    assert caught.value.code == "folder_missing"
    with pytest.raises(PushError) as caught:
        asyncio.run(thread_push.accept_push(manager, {"backend": "codex"}))
    assert caught.value.http_status == 400
    assert receiving["imports"] == 0


def test_accept_waits_for_the_snapshot_then_gives_up(
    isolated, receiving, monkeypatch
) -> None:
    monkeypatch.setattr(thread_handoff, "snapshot_present", lambda snapshot: False)
    monkeypatch.setattr(thread_push, "SNAPSHOT_WAIT_SECONDS", 0)

    with pytest.raises(PushError) as caught:
        asyncio.run(thread_push.accept_push(FakeManager({}), _arrival()))

    assert caught.value.code == "snapshot_missing"
    assert caught.value.safe_to_retry


def test_accept_refuses_a_thread_running_here(isolated, receiving) -> None:
    running = TurnInfo(run_id="r", started_at=datetime.now())
    manager = FakeManager({"t-1": _thread(current_run=running)})

    with pytest.raises(PushError) as caught:
        asyncio.run(thread_push.accept_push(manager, _arrival()))

    assert caught.value.code == "target_busy"
    assert receiving["imports"] == 0
