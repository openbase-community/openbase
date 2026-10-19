"""The Dispatcher thread has exactly one owning Codex app-server.

Typed Dispatcher turns from the apps run in django-cli; the voice worker and
the restart runner hold the same thread on the dedicated dispatcher
app-server. Codex keeps a per-thread writer lock per process, so django-cli
must talk to the dedicated instance for that thread (switch-speed, 2026-10-18).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from openbase_coder_cli import codex_control_plane
from openbase_coder_cli.thread_sync import session_manager as session_manager_module
from openbase_coder_cli.thread_sync.session_manager import CodexAppServerSessionManager


class _Request:
    def __init__(
        self, request_id: str, method: str = "item/commandExecution/requestApproval"
    ) -> None:
        self.id = request_id
        self.method = method
        self.params: dict[str, Any] = {"threadId": "thread", "turnId": "turn"}

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "method": self.method, "params": self.params}


class _Client:
    def __init__(self, pending: list[_Request] | None = None) -> None:
        self.pending = pending or []
        self.answered: list[tuple[str, dict[str, Any]]] = []

    async def ensure_connected(self) -> None:
        return None

    async def read_thread(
        self, thread_id: str, include_turns: bool = True
    ) -> dict[str, Any]:
        return {}

    def pending_permission_requests(self) -> list[_Request]:
        return list(self.pending)

    async def answer_request(
        self, request_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        self.answered.append((request_id, result))
        return {"answered": True}


def _manager(shared: Any, dispatcher: Any | None) -> CodexAppServerSessionManager:
    return CodexAppServerSessionManager(
        client=shared,
        dispatcher_client=dispatcher,
        model_for_role=lambda _role: None,
    )


def test_dispatcher_thread_uses_the_dedicated_client(monkeypatch) -> None:
    monkeypatch.setattr(
        session_manager_module,
        "is_dispatcher_thread",
        lambda thread_id: thread_id == "disp",
    )
    shared, dispatcher = _Client(), _Client()
    manager = _manager(shared, dispatcher)

    assert manager._client_for_thread("disp") is dispatcher
    assert manager._client_for_thread("worker") is shared
    assert manager._codex_clients() == [shared, dispatcher]


def test_dispatcher_thread_falls_back_to_the_shared_client_without_a_dedicated_one(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        session_manager_module, "is_dispatcher_thread", lambda _id: True
    )
    shared = _Client()
    manager = _manager(shared, None)

    # An injected (non app-server) client never grows a dedicated sibling.
    assert manager._client_for_thread("disp") is shared
    assert manager._codex_clients() == [shared]


def test_approval_requests_merge_and_answer_across_both_clients(monkeypatch) -> None:
    monkeypatch.setattr(
        session_manager_module, "is_dispatcher_thread", lambda _id: False
    )
    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.session_manager_approvals.shared_permission_requests",
        lambda: [],
    )
    shared = _Client([_Request("1")])
    dispatcher = _Client([_Request("2")])
    manager = _manager(shared, dispatcher)

    listed = asyncio.run(manager.list_approval_requests())
    assert sorted(str(item["id"]) for item in listed) == ["1", "2"]

    asyncio.run(manager.answer_approval_request("2", "accept"))
    assert [request_id for request_id, _ in dispatcher.answered] == ["2"]
    assert shared.answered == []


def test_usable_dispatcher_endpoint_prefers_explicit_then_live_socket(
    tmp_path: Path,
) -> None:
    env = {"CODEX_HOME": str(tmp_path)}
    # Default socket absent: the shared instance keeps the thread.
    assert codex_control_plane.usable_dispatcher_codex_app_server_endpoint(env) is None

    socket_path = tmp_path / "app-server-control-dispatcher" / "app-server-control.sock"
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()  # the helper only checks that the socket path exists
    assert codex_control_plane.usable_dispatcher_codex_app_server_endpoint(env) == (
        f"unix://{socket_path}"
    )
    assert (
        codex_control_plane.usable_dispatcher_codex_app_server_endpoint(
            {**env, "OPENBASE_DISPATCHER_DEDICATED_APP_SERVER": "off"}
        )
        is None
    )

    explicit = {
        **env,
        codex_control_plane.DISPATCHER_APP_SERVER_ENDPOINT_ENV: "ws://127.0.0.1:7997",
    }
    assert (
        codex_control_plane.usable_dispatcher_codex_app_server_endpoint(explicit)
        == "ws://127.0.0.1:7997"
    )
