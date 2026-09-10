from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import openbase_coder_cli.thread_sync.session_manager as session_manager_module
from openbase_coder_cli.thread_sync.session_manager import (
    CodexAppServerSessionManager,
    resolve_super_agent_instructions_path,
)


def _thread(
    thread_id: str,
    cwd: str,
    *,
    created_at: int = 1_778_160_000,
    updated_at: int | None = None,
    status: str | dict[str, Any] = "notLoaded",
    turns: list[dict[str, Any]] | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    updated_at = updated_at if updated_at is not None else created_at
    payload: dict[str, Any] = {
        "id": thread_id,
        "preview": "preview",
        "createdAt": created_at,
        "updatedAt": updated_at,
        "status": status if isinstance(status, dict) else {"type": status},
        "cwd": cwd,
        "turns": turns or [],
    }
    if name:
        payload["name"] = name
    return payload


def test_resolve_super_agent_instructions_path_defaults_to_openbase_codex_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    default_path = tmp_path / "codex_home" / "SUPER_AGENT_INSTRUCTIONS.md"
    monkeypatch.delenv("CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", raising=False)

    assert (
        resolve_super_agent_instructions_path(default_path=default_path) == default_path
    )


def test_resolve_super_agent_instructions_path_honors_env_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    override_path = tmp_path / "override" / "SUPER_AGENT_INSTRUCTIONS.md"
    monkeypatch.setenv("CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", str(override_path))

    assert resolve_super_agent_instructions_path() == override_path


def test_configured_execution_backend_prefers_env_file_over_stale_process_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from openbase_coder_cli import paths

    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=openbase_cloud\n", encoding="utf-8")
    monkeypatch.setattr(paths, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "codex")

    assert session_manager_module._configured_execution_backend() == "claude_code"


def test_get_session_manager_reloads_when_configured_backend_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from openbase_coder_cli import paths

    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=codex\n", encoding="utf-8")
    monkeypatch.setattr(paths, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "codex")
    monkeypatch.setattr(session_manager_module, "_session_manager", None)
    created_backends: list[str] = []

    def fake_default_client_for_execution_backend(**kwargs):
        created_backends.append(kwargs["execution_backend"])
        return SimpleNamespace()

    monkeypatch.setattr(
        session_manager_module,
        "_default_client_for_execution_backend",
        fake_default_client_for_execution_backend,
    )

    first = session_manager_module.get_session_manager()

    env_file.write_text("OPENBASE_CODING_BACKEND=openbase_cloud\n", encoding="utf-8")
    second = session_manager_module.get_session_manager()

    assert first is not second
    assert created_backends == ["codex", "claude_code"]


def _turn(
    turn_id: str,
    *,
    message: str,
    output: str,
    started_at: int,
    completed_at: int | None,
    status: str,
) -> dict[str, Any]:
    return {
        "id": turn_id,
        "status": status,
        "startedAt": started_at,
        "completedAt": completed_at,
        "durationMs": 1,
        "error": None,
        "items": [
            {
                "type": "userMessage",
                "id": "item-user",
                "content": [{"type": "text", "text": message}],
            },
            {
                "type": "agentMessage",
                "id": "item-agent",
                "phase": "final",
                "text": output,
            },
        ],
    }


class FakeSuperAgentsClient:
    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[tuple[str, Any]] = []
        self.merged: list[tuple[str, dict[str, Any]]] = []

    async def ensure_connected(self) -> None:
        self.calls.append(("ensure_connected", {}))

    async def list_threads(
        self,
        use_state_db_only: bool = True,
        search_term: str | None = None,
        cwd: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "list_threads",
                {
                    "use_state_db_only": use_state_db_only,
                    "search_term": search_term,
                    "cwd": cwd,
                    "limit": limit,
                    "cursor": cursor,
                },
            )
        )
        return self._pop("list_threads")

    async def read_thread(
        self,
        thread_id: str,
        include_turns: bool = True,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "read_thread",
                {"thread_id": thread_id, "include_turns": include_turns},
            )
        )
        return self._pop("read_thread")

    async def get_session(self, thread_id: str) -> Any:
        self.calls.append(("get_session", {"thread_id": thread_id}))
        if "get_session" not in self.responses:
            return None
        return self._pop("get_session")

    async def start_thread(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("start_thread", input_data))
        return self._pop("start_thread")

    async def start_turn(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("start_turn", input_data))
        return self._pop("start_turn")

    async def start_turn_by_label(
        self,
        input_data,
        turn_input: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "start_turn_by_label",
                {
                    "thread_id": input_data.thread_id,
                    "cwd": input_data.cwd,
                    "turn_id": input_data.turn_id,
                    "turn_input": turn_input,
                },
            )
        )
        return self._pop("start_turn_by_label")

    async def queue_turn_by_label(
        self,
        input_data,
        turn_input: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "queue_turn_by_label",
                {
                    "thread_id": input_data.thread_id,
                    "cwd": input_data.cwd,
                    "turn_input": turn_input,
                },
            )
        )
        return self._pop("queue_turn_by_label")

    async def steer_by_label(
        self,
        input_data,
        prompt: str,
        turn_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "steer_by_label",
                {
                    "thread_id": input_data.thread_id,
                    "cwd": input_data.cwd,
                    "turn_id": input_data.turn_id,
                    "prefer": input_data.prefer,
                    "prompt": prompt,
                    "turn_input": turn_input,
                },
            )
        )
        return self._pop("steer_by_label")

    async def cancel_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        self.calls.append(("cancel_turn", {"thread_id": thread_id, "turn_id": turn_id}))
        return self._pop("cancel_turn")

    def pending_permission_requests(self) -> list[Any]:
        self.calls.append(("pending_permission_requests", {}))
        return self._pop("pending_permission_requests")

    async def answer_request(
        self,
        request_id: str | int,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            ("answer_request", {"request_id": request_id, "result": result})
        )
        return self._pop("answer_request")

    async def save_routine(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("save_routine", input_data))
        return self._pop("save_routine")

    async def list_routines(self) -> dict[str, Any]:
        self.calls.append(("list_routines", {}))
        return self._pop("list_routines")

    async def read_routine(self, name: str) -> dict[str, Any]:
        self.calls.append(("read_routine", {"name": name}))
        return self._pop("read_routine")

    async def delete_routine(self, name: str) -> dict[str, Any]:
        self.calls.append(("delete_routine", {"name": name}))
        return self._pop("delete_routine")

    async def run_due_routines(
        self,
        name: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(("run_due_routines", {"name": name, "force": force}))
        return self._pop("run_due_routines")

    async def add_routine_trigger(
        self, name: str, trigger_input: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("add_routine_trigger", {"name": name, **trigger_input}))
        return self._pop("add_routine_trigger")

    async def remove_routine_trigger(
        self, name: str, trigger_id: str
    ) -> dict[str, Any]:
        self.calls.append(
            ("remove_routine_trigger", {"name": name, "triggerId": trigger_id})
        )
        return self._pop("remove_routine_trigger")

    async def deliver_webhook_event(
        self,
        token: str,
        *,
        headers: dict[str, Any] | None = None,
        body: bytes | str = b"",
        origin: str = "external",
    ) -> dict[str, Any]:
        self.calls.append(("deliver_webhook_event", {"token": token, "origin": origin}))
        return self._pop("deliver_webhook_event")

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
        return self._pop("emit_routine_event")

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: float = 30,
    ) -> dict[str, Any]:
        self.calls.append(("request", {"method": method, "params": params}))
        return self._pop(f"request:{method}")

    async def merge_session(
        self,
        thread_id: str,
        patch: dict[str, Any],
        *,
        clear_fields: list[str] | None = None,
    ) -> None:
        self.calls.append(("merge_session", {"thread_id": thread_id, "patch": patch}))
        self.merged.append((thread_id, patch))

    def _pop(self, key: str) -> Any:
        queue = self.responses.get(key, [])
        if not queue:
            raise AssertionError(f"Unexpected fake client call: {key}")
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeBackendSessionClient:
    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[tuple[str, Any]] = []

    async def sessions(self) -> list[dict[str, Any]]:
        self.calls.append(("sessions", {}))
        return self._pop("sessions")

    async def read_by_label(
        self, input_data, include_turns: bool = False
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "read_by_label",
                {
                    "thread_id": input_data.thread_id,
                    "cwd": input_data.cwd,
                    "include_turns": include_turns,
                    "max_items": input_data.max_items,
                },
            )
        )
        return self._pop("read_by_label")

    async def start_thread(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("start_thread", input_data))
        return self._pop("start_thread")

    async def resume_by_label(
        self,
        input_data,
        *,
        developer_instructions: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "resume_by_label",
                {
                    "thread_id": input_data.thread_id,
                    "cwd": input_data.cwd,
                    "developer_instructions": developer_instructions,
                },
            )
        )
        return self._pop("resume_by_label")

    async def start_turn_by_label(
        self,
        input_data,
        turn_input: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "start_turn_by_label",
                {"thread_id": input_data.thread_id, "turn_input": turn_input},
            )
        )
        return self._pop("start_turn_by_label")

    async def queue_turn_by_label(
        self,
        input_data,
        turn_input: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "queue_turn_by_label",
                {"thread_id": input_data.thread_id, "turn_input": turn_input},
            )
        )
        return self._pop("queue_turn_by_label")

    async def steer_by_label(
        self,
        input_data,
        prompt: str,
        turn_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "steer_by_label",
                {
                    "thread_id": input_data.thread_id,
                    "turn_id": input_data.turn_id,
                    "prompt": prompt,
                    "turn_input": turn_input,
                },
            )
        )
        return self._pop("steer_by_label")

    async def cancel_by_label(self, input_data) -> dict[str, Any]:
        self.calls.append(("cancel_by_label", {"thread_id": input_data.thread_id}))
        return self._pop("cancel_by_label")

    def _pop(self, key: str) -> Any:
        queue = self.responses.get(key, [])
        if not queue:
            raise AssertionError(f"Unexpected fake client call: {key}")
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _manager(
    client: Any,
    routine_client: Any | None = None,
) -> CodexAppServerSessionManager:
    return CodexAppServerSessionManager(
        client=client,
        routine_client=routine_client,
        model_for_role=lambda _role: None,
    )


def test_list_threads_reads_threads_from_super_agents(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeSuperAgentsClient(
        {
            "list_threads": [
                {"data": [_thread("thr-1", str(project_dir), name="Project thread")]}
            ]
        }
    )

    threads = asyncio.run(_manager(client).list_threads())

    assert len(threads) == 1
    assert threads[0].session_id == "thr-1"
    assert threads[0].directory == str(project_dir)
    assert threads[0].name == "Project thread"
    assert threads[0].model_dump(mode="json")["status"] == "idle"


def test_list_approval_requests_reads_current_super_agents_pending_requests(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(
        "SUPER_AGENTS_APPROVAL_REQUESTS_FILE", str(tmp_path / "approvals.json")
    )
    request = SimpleNamespace(
        id="approval-1",
        method="exec/requestApproval",
        params={"threadId": "thr-1", "turnId": "turn-1", "command": "make test"},
        to_json=lambda: {
            "id": "approval-1",
            "method": "exec/requestApproval",
            "params": {
                "threadId": "thr-1",
                "turnId": "turn-1",
                "command": "make test",
            },
            "receivedAt": "2026-05-23T00:00:00.000Z",
        },
    )
    client = FakeSuperAgentsClient({"pending_permission_requests": [[request]]})

    requests = asyncio.run(_manager(client).list_approval_requests())

    assert requests == [
        {
            "id": "approval-1",
            "method": "exec/requestApproval",
            "params": {
                "threadId": "thr-1",
                "turnId": "turn-1",
                "command": "make test",
            },
            "received_at": "2026-05-23T00:00:00.000Z",
            "thread_id": "thr-1",
            "turn_id": "turn-1",
        }
    ]
    assert client.calls[0] == ("ensure_connected", {})


def test_answer_approval_request_sends_accept_decision() -> None:
    request = SimpleNamespace(
        id="approval-1",
        method="exec/requestApproval",
        params={},
        to_json=lambda: {
            "id": "approval-1",
            "method": "exec/requestApproval",
            "params": {},
        },
    )
    client = FakeSuperAgentsClient(
        {
            "pending_permission_requests": [[request]],
            "answer_request": [{"answered": True}],
        }
    )

    result = asyncio.run(
        _manager(client).answer_approval_request("approval-1", "accept")
    )

    assert result == {"answered": True}
    assert client.calls[-1] == (
        "answer_request",
        {"request_id": "approval-1", "result": {"decision": "accept"}},
    )


def test_answer_approval_request_sends_elicitation_action() -> None:
    request = SimpleNamespace(
        id="elicitation-1",
        method="mcpServer/elicitation/request",
        params={},
        to_json=lambda: {
            "id": "elicitation-1",
            "method": "mcpServer/elicitation/request",
            "params": {},
        },
    )
    client = FakeSuperAgentsClient(
        {
            "pending_permission_requests": [[request]],
            "answer_request": [{"answered": True}],
        }
    )

    result = asyncio.run(
        _manager(client).answer_approval_request("elicitation-1", "accept")
    )

    assert result == {"answered": True}
    assert client.calls[-1] == (
        "answer_request",
        {
            "request_id": "elicitation-1",
            "result": {"action": "accept", "content": None, "_meta": None},
        },
    )


def test_answer_approval_request_queues_shared_decision_for_external_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    approvals_path = tmp_path / "approvals.json"
    approvals_path.write_text(
        '{"requests":{"approval-1":{"id":"approval-1","method":"exec/requestApproval","params":{}}},"decisions":{}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("SUPER_AGENTS_APPROVAL_REQUESTS_FILE", str(approvals_path))
    client = FakeSuperAgentsClient({"pending_permission_requests": [[]]})

    result = asyncio.run(
        _manager(client).answer_approval_request("approval-1", "decline")
    )

    assert result["queued"] is True
    assert result["result"] == {"decision": "decline"}
    assert '"decision": "decline"' in approvals_path.read_text(encoding="utf-8")


def test_answer_shared_approval_without_app_server_connection_method(
    tmp_path: Path,
    monkeypatch,
) -> None:
    approvals_path = tmp_path / "approvals.json"
    approvals_path.write_text(
        '{"requests":{"gmail-cli:1":{"id":"gmail-cli:1","method":"exec/requestApproval","params":{}}},"decisions":{}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("SUPER_AGENTS_APPROVAL_REQUESTS_FILE", str(approvals_path))
    client = SimpleNamespace(
        pending_permission_requests=lambda: [],
        answer_request=None,
    )

    result = asyncio.run(
        _manager(client).answer_approval_request("gmail-cli:1", "accept")
    )

    assert result["queued"] is True
    assert '"decision": "accept"' in approvals_path.read_text(encoding="utf-8")


def test_answer_approval_request_queues_elicitation_action_for_external_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    approvals_path = tmp_path / "approvals.json"
    approvals_path.write_text(
        '{"requests":{"elicitation-1":{"id":"elicitation-1","method":"mcpServer/elicitation/request","params":{}}},"decisions":{}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("SUPER_AGENTS_APPROVAL_REQUESTS_FILE", str(approvals_path))
    client = FakeSuperAgentsClient({"pending_permission_requests": [[]]})

    result = asyncio.run(
        _manager(client).answer_approval_request("elicitation-1", "accept")
    )

    assert result["queued"] is True
    assert result["result"] == {"action": "accept", "content": None, "_meta": None}
    assert '"decision": "accept"' in approvals_path.read_text(encoding="utf-8")


def test_list_approval_requests_hides_requests_with_queued_decisions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    approvals_path = tmp_path / "approvals.json"
    approvals_path.write_text(
        '{"requests":{"approval-1":{"id":"approval-1","method":"exec/requestApproval","params":{}}},"decisions":{"approval-1":{"decision":"accept"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("SUPER_AGENTS_APPROVAL_REQUESTS_FILE", str(approvals_path))
    client = FakeSuperAgentsClient({"pending_permission_requests": [[]]})

    requests = asyncio.run(_manager(client).list_approval_requests())

    assert requests == []


def test_routine_methods_delegate_to_super_agents_client() -> None:
    client = FakeSuperAgentsClient(
        {
            "list_routines": [{"routines": []}],
            "save_routine": [{"routine": {"name": "daily"}}],
            "read_routine": [{"routine": {"name": "daily"}}],
            "run_due_routines": [{"count": 1, "results": [{"name": "daily"}]}],
            "delete_routine": [{"deleted": True}],
        }
    )
    manager = _manager(client)

    assert asyncio.run(manager.list_routines()) == {"routines": []}
    assert asyncio.run(manager.save_routine({"name": "daily"})) == {
        "routine": {"name": "daily"}
    }
    assert asyncio.run(manager.read_routine("daily")) == {"routine": {"name": "daily"}}
    assert asyncio.run(manager.run_due_routines(name="daily", force=True)) == {
        "count": 1,
        "results": [{"name": "daily"}],
    }
    assert asyncio.run(manager.delete_routine("daily")) == {"deleted": True}
    assert client.calls[-5:] == [
        ("list_routines", {}),
        ("save_routine", {"name": "daily"}),
        ("read_routine", {"name": "daily"}),
        ("run_due_routines", {"name": "daily", "force": True}),
        ("delete_routine", {"name": "daily"}),
    ]


def test_routine_methods_use_dedicated_client_when_backend_lacks_routines() -> None:
    backend_client = SimpleNamespace()
    routine_client = FakeSuperAgentsClient(
        {
            "list_routines": [{"routines": []}],
        }
    )
    manager = _manager(backend_client, routine_client=routine_client)

    assert asyncio.run(manager.list_routines()) == {"routines": []}
    assert routine_client.calls == [("list_routines", {})]


def test_list_threads_paginates_super_agents_results(tmp_path: Path) -> None:
    first_project_dir = tmp_path / "first-project"
    second_project_dir = tmp_path / "second-project"
    first_project_dir.mkdir()
    second_project_dir.mkdir()
    client = FakeSuperAgentsClient(
        {
            "list_threads": [
                {
                    "data": [
                        _thread("thr-1", str(first_project_dir), updated_at=10),
                    ],
                    "nextCursor": "cursor-2",
                }
            ],
            "request:thread/list": [
                {
                    "data": [
                        _thread("thr-2", str(second_project_dir), updated_at=20),
                    ],
                    "nextCursor": None,
                }
            ],
        }
    )

    threads = asyncio.run(_manager(client).list_threads())

    assert [thread.session_id for thread in threads] == ["thr-2", "thr-1"]
    assert client.calls[1] == ("ensure_connected", {})
    assert client.calls[2] == (
        "request",
        {
            "method": "thread/list",
            "params": {
                "useStateDbOnly": True,
                "limit": 100,
                "cursor": "cursor-2",
                "modelProviders": [],
            },
        },
    )


def test_list_thread_page_ranks_creation_ordered_pages_by_recency(
    tmp_path: Path,
) -> None:
    """thread/list pages arrive in creation order; ranking is by update time."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeSuperAgentsClient(
        {
            "list_threads": [
                {
                    "data": [
                        _thread(
                            "thr-new-idle",
                            str(project_dir),
                            created_at=1_778_200_000,
                            updated_at=1_778_200_100,
                            name="Created recently",
                        )
                    ],
                    "nextCursor": "cursor-2",
                }
            ],
            "request:thread/list": [
                {
                    "data": [
                        _thread(
                            "thr-old-active",
                            str(project_dir),
                            created_at=1_778_100_000,
                            updated_at=1_778_300_000,
                            name="Created long ago, touched today",
                        )
                    ],
                    "nextCursor": None,
                }
            ],
        }
    )

    page = asyncio.run(_manager(client).list_thread_page(limit=25))

    assert [thread.session_id for thread in page.threads] == [
        "thr-old-active",
        "thr-new-idle",
    ]
    assert page.next_cursor is None
    assert client.calls[0] == (
        "list_threads",
        {
            "use_state_db_only": True,
            "search_term": None,
            "cwd": None,
            "limit": 100,
            "cursor": None,
        },
    )
    assert (
        "request",
        {
            "method": "thread/list",
            "params": {
                "useStateDbOnly": True,
                "limit": 100,
                "cursor": "cursor-2",
                "modelProviders": [],
            },
        },
    ) in client.calls


def test_list_thread_page_resumes_from_offset_cursor(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeSuperAgentsClient(
        {
            "list_threads": [
                {
                    "data": [
                        _thread(
                            "thr-1",
                            str(project_dir),
                            updated_at=1_778_200_000,
                            name="First",
                        ),
                        _thread(
                            "thr-2",
                            str(project_dir),
                            updated_at=1_778_100_000,
                            name="Second",
                        ),
                    ],
                    "nextCursor": None,
                }
            ]
        }
    )

    page = asyncio.run(_manager(client).list_thread_page(limit=1, cursor="1"))

    assert [thread.session_id for thread in page.threads] == ["thr-2"]
    assert page.next_cursor is None


def test_list_thread_page_reads_claude_code_backend_sessions(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeBackendSessionClient(
        {
            "sessions": [
                [
                    {
                        "id": "s_dispatcher",
                        "name": "dispatcher",
                        "cwd": str(project_dir),
                        "status": "waiting",
                        "createdAt": "2026-06-19T20:00:00.000Z",
                        "updatedAt": "2026-06-19T21:00:00.000Z",
                        "lastTurnId": "t_1",
                    }
                ]
            ]
        }
    )

    page = asyncio.run(_manager(client).list_thread_page(limit=25))

    assert [thread.session_id for thread in page.threads] == ["s_dispatcher"]
    assert page.threads[0].name == "dispatcher"
    assert page.threads[0].directory == str(project_dir)
    assert page.threads[0].status == "waiting"
    assert page.next_cursor is None
    assert client.calls == [("sessions", {})]


def test_read_thread_reads_claude_code_backend_turns(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeBackendSessionClient(
        {
            "read_by_label": [
                {
                    "threadId": "s_dispatcher",
                    "backend": "claude_code",
                    "session": {
                        "id": "s_dispatcher",
                        "name": "dispatcher",
                        "cwd": str(project_dir),
                        "status": "waiting",
                        "createdAt": "2026-06-19T20:00:00.000Z",
                        "updatedAt": "2026-06-19T21:00:00.000Z",
                    },
                    "turns": [
                        {
                            "turnId": "t_1",
                            "promptPreview": "Say hi",
                            "status": "completed",
                            "createdAt": "2026-06-19T20:05:00.000Z",
                            "finishedAt": "2026-06-19T20:05:10.000Z",
                            "reasoningEffort": "low",
                            "lastUsefulMessage": "Hi from Claude Code.",
                        }
                    ],
                }
            ]
        }
    )

    thread = asyncio.run(_manager(client).get_thread_state("s_dispatcher"))

    assert thread is not None
    assert thread.session_id == "s_dispatcher"
    assert thread.name == "dispatcher"
    assert thread.status == "waiting"
    assert [turn.run_id for turn in thread.run_history] == ["t_1"]
    assert thread.run_history[0].message == "Say hi"
    assert thread.run_history[0].accumulated_output == "Hi from Claude Code."
    assert thread.run_history[0].reasoning_effort == "low"
    assert client.calls == [
        (
            "read_by_label",
            {
                "thread_id": "s_dispatcher",
                "cwd": None,
                "include_turns": True,
                "max_items": 25,
            },
        )
    ]


def test_read_thread_treats_invalid_backend_thread_id_as_missing() -> None:
    client = FakeBackendSessionClient(
        {
            "read_by_label": [
                RuntimeError(
                    '{"code": -32600, "message": '
                    '"invalid thread id: invalid character"}'
                )
            ]
        }
    )

    thread = asyncio.run(_manager(client).get_thread_state("s_stale"))

    assert thread is None
    assert client.calls == [
        (
            "read_by_label",
            {
                "thread_id": "s_stale",
                "cwd": None,
                "include_turns": True,
                "max_items": 25,
            },
        )
    ]


def test_read_thread_treats_backend_label_resolution_error_as_missing() -> None:
    client = FakeBackendSessionClient(
        {"read_by_label": [ValueError("label must be a non-empty string.")]}
    )

    thread = asyncio.run(_manager(client).get_thread_state("s_stale"))

    assert thread is None
    assert client.calls == [
        (
            "read_by_label",
            {
                "thread_id": "s_stale",
                "cwd": None,
                "include_turns": True,
                "max_items": 25,
            },
        )
    ]


def test_resume_thread_without_developer_instructions_is_backend_session_noop(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeBackendSessionClient(
        {
            "read_by_label": [
                {
                    "threadId": "s_grace",
                    "backend": "claude_code",
                    "session": {
                        "id": "s_grace",
                        "name": "elon-musk-report",
                        "agentName": "Grace",
                        "cwd": str(project_dir),
                        "status": "waiting",
                    },
                }
            ]
        }
    )

    asyncio.run(
        _manager(client).resume_thread_without_developer_instructions(
            "s_grace",
            str(project_dir),
        )
    )

    assert client.calls == [
        (
            "read_by_label",
            {
                "thread_id": "s_grace",
                "cwd": str(project_dir),
                "include_turns": False,
                "max_items": None,
            },
        )
    ]


def test_resume_thread_with_developer_instructions_updates_backend_session(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeBackendSessionClient(
        {
            "resume_by_label": [
                {
                    "threadId": "s_grace",
                    "backend": "claude_code",
                    "session": {
                        "id": "s_grace",
                        "name": "elon-musk-report",
                        "agentName": "Grace",
                        "cwd": str(project_dir),
                        "status": "waiting",
                    },
                }
            ]
        }
    )

    asyncio.run(
        _manager(client).resume_thread_with_developer_instructions(
            "s_grace",
            str(project_dir),
            "direct voice instructions",
        )
    )

    assert client.calls == [
        (
            "resume_by_label",
            {
                "thread_id": "s_grace",
                "cwd": str(project_dir),
                "developer_instructions": "direct voice instructions",
            },
        )
    ]


def test_read_thread_ignores_stale_claude_code_running_turns(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeBackendSessionClient(
        {
            "read_by_label": [
                {
                    "threadId": "s_dispatcher",
                    "backend": "claude_code",
                    "session": {
                        "id": "s_dispatcher",
                        "name": "dispatcher",
                        "cwd": str(project_dir),
                        "status": "waiting",
                        "createdAt": "2026-06-19T20:00:00.000Z",
                        "updatedAt": "2026-06-19T21:00:00.000Z",
                    },
                    "turns": [
                        {
                            "turnId": "t_stale",
                            "promptPreview": "stale partial voice transcript",
                            "status": "running",
                            "createdAt": "2026-06-19T20:05:00.000Z",
                        },
                        {
                            "turnId": "t_done",
                            "promptPreview": "completed prompt",
                            "status": "completed",
                            "createdAt": "2026-06-19T20:10:00.000Z",
                            "finishedAt": "2026-06-19T20:10:10.000Z",
                        },
                    ],
                }
            ]
        }
    )

    thread = asyncio.run(_manager(client).get_thread_state("s_dispatcher"))

    assert thread is not None
    assert thread.status == "waiting"
    assert thread.current_run is None
    assert [turn.run_id for turn in thread.run_history] == ["t_done"]


def test_read_thread_uses_claude_code_active_turn_id(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeBackendSessionClient(
        {
            "read_by_label": [
                {
                    "threadId": "s_dispatcher",
                    "backend": "claude_code",
                    "session": {
                        "id": "s_dispatcher",
                        "name": "dispatcher",
                        "cwd": str(project_dir),
                        "status": "running",
                        "activeTurnId": "t_active",
                        "createdAt": "2026-06-19T20:00:00.000Z",
                        "updatedAt": "2026-06-19T21:00:00.000Z",
                    },
                    "turns": [
                        {
                            "turnId": "t_stale",
                            "promptPreview": "older running row",
                            "status": "running",
                            "createdAt": "2026-06-19T20:05:00.000Z",
                        },
                        {
                            "turnId": "t_active",
                            "promptPreview": "actual active prompt",
                            "status": "running",
                            "createdAt": "2026-06-19T20:10:00.000Z",
                        },
                    ],
                }
            ]
        }
    )

    thread = asyncio.run(_manager(client).get_thread_state("s_dispatcher"))

    assert thread is not None
    assert thread.status == "running"
    assert thread.current_run is not None
    assert thread.current_run.run_id == "t_active"
    assert thread.current_run.message == "actual active prompt"


def test_read_thread_exposes_stale_active_turn_warning(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeBackendSessionClient(
        {
            "read_by_label": [
                {
                    "threadId": "s_dispatcher",
                    "backend": "codex",
                    "session": {
                        "id": "s_dispatcher",
                        "name": "dispatcher",
                        "cwd": str(project_dir),
                        "status": "unknown",
                        "activeTurnId": "t_orphan",
                        "statusWarning": "stale_active_turn",
                        "isLikelyStale": True,
                        "createdAt": "2026-06-30T22:00:00.000Z",
                        "updatedAt": "2026-06-30T22:15:00.000Z",
                    },
                    "turns": [
                        {
                            "turnId": "t_orphan",
                            "promptPreview": "hey are you there",
                            "status": "running",
                            "createdAt": "2026-06-30T22:13:13.000Z",
                        },
                    ],
                }
            ]
        }
    )

    thread = asyncio.run(_manager(client).get_thread_state("s_dispatcher"))

    assert thread is not None
    assert thread.status == "idle"
    assert thread.current_run is None
    assert thread.is_likely_stale is True
    assert thread.status_warning == "stale_active_turn"
    assert [turn.run_id for turn in thread.run_history] == ["t_orphan"]


def test_create_session_includes_super_agent_instructions_for_backend_sessions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    instructions_path = tmp_path / "SUPER_AGENT_INSTRUCTIONS.md"
    instructions_path.write_text("super backend instructions\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", str(instructions_path))
    client = FakeBackendSessionClient(
        {
            "sessions": [[]],
            "start_thread": [
                {
                    "id": "s_backend",
                    "name": "project",
                    "cwd": str(project_dir),
                    "status": "waiting",
                }
            ],
        }
    )

    manager = CodexAppServerSessionManager(
        client=client,
        model_for_role=lambda role: (
            "gpt-super-agent" if role == "super_agents" else "gpt-dispatcher"
        ),
    )
    thread = asyncio.run(manager.create_session(str(project_dir)))

    assert thread.session_id == "s_backend"
    assert client.calls == [
        ("sessions", {}),
        (
            "start_thread",
            {
                "name": "project",
                "cwd": str(project_dir.resolve()),
                "model": "gpt-super-agent",
                "developerInstructions": "super backend instructions",
            },
        ),
    ]


def test_create_session_omits_missing_super_agent_instructions_for_backend_sessions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setenv(
        "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH",
        str(tmp_path / "missing" / "SUPER_AGENT_INSTRUCTIONS.md"),
    )
    client = FakeBackendSessionClient(
        {
            "sessions": [[]],
            "start_thread": [
                {
                    "id": "s_backend",
                    "name": "project",
                    "cwd": str(project_dir),
                    "status": "waiting",
                }
            ],
        }
    )

    thread = asyncio.run(_manager(client).create_session(str(project_dir)))

    assert thread.session_id == "s_backend"
    assert client.calls == [
        ("sessions", {}),
        (
            "start_thread",
            {
                "name": "project",
                "cwd": str(project_dir.resolve()),
            },
        ),
    ]


def test_create_thread_requests_fresh_backend_session_when_directory_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setenv(
        "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH",
        str(tmp_path / "missing" / "SUPER_AGENT_INSTRUCTIONS.md"),
    )
    client = FakeBackendSessionClient(
        {
            "sessions": [
                [
                    {
                        "id": "s_existing",
                        "name": "project",
                        "cwd": str(project_dir.resolve()),
                        "status": "idle",
                    }
                ]
            ],
            "start_thread": [
                {
                    "id": "s_new",
                    "name": "project",
                    "cwd": str(project_dir),
                    "status": "waiting",
                }
            ],
        }
    )

    thread = asyncio.run(_manager(client).create_thread(str(project_dir)))

    assert thread.session_id == "s_new"
    assert client.calls == [
        ("sessions", {}),
        (
            "start_thread",
            {
                "name": "project",
                "cwd": str(project_dir.resolve()),
                "fresh": True,
            },
        ),
    ]


def test_send_message_starts_claude_code_backend_turn(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeBackendSessionClient(
        {
            "read_by_label": [
                {
                    "threadId": "s_dispatcher",
                    "session": {
                        "id": "s_dispatcher",
                        "name": "dispatcher",
                        "cwd": str(project_dir),
                        "status": "waiting",
                        "createdAt": "2026-06-19T20:00:00.000Z",
                        "updatedAt": "2026-06-19T21:00:00.000Z",
                    },
                }
            ],
            "start_turn_by_label": [{"turnId": "t_2"}],
        }
    )

    manager = CodexAppServerSessionManager(
        client=client,
        model_for_role=lambda role: (
            "gpt-dispatcher" if role == "dispatcher" else "gpt-super-agent"
        ),
    )
    turn_id = asyncio.run(manager.send_message("s_dispatcher", "Continue"))

    assert turn_id == "t_2"
    assert client.calls[-1] == (
        "start_turn_by_label",
        {
            "thread_id": "s_dispatcher",
            "turn_input": {
                "prompt": "Continue",
                "cwd": str(project_dir),
                "model": "gpt-dispatcher",
            },
        },
    )


def test_list_threads_marks_waiting_on_user_input_as_waiting(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    waiting_status = {"type": "active", "activeFlags": ["waitingOnUserInput"]}
    client = FakeSuperAgentsClient(
        {
            "list_threads": [
                {"data": [_thread("thr-1", str(project_dir), status=waiting_status)]}
            ]
        }
    )

    threads = asyncio.run(_manager(client).list_threads())

    assert threads[0].status == "waiting"
    assert threads[0].current_run is None
    assert threads[0].model_dump(mode="json")["status"] == "waiting"


def test_read_thread_treats_in_progress_turn_as_current_run(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeSuperAgentsClient(
        {
            "read_thread": [
                {
                    "thread": _thread(
                        "thr-1",
                        str(project_dir),
                        status="active",
                        turns=[
                            _turn(
                                "turn-1",
                                message="Inspect repo",
                                output="Working",
                                started_at=10,
                                completed_at=None,
                                status="inProgress",
                            )
                        ],
                    )
                }
            ]
        }
    )

    thread = asyncio.run(_manager(client).get_thread_state("thr-1"))

    assert thread is not None
    assert thread.status == "running"
    assert thread.current_run is not None
    assert thread.current_run.run_id == "turn-1"
    assert thread.current_run.status == "running"


def test_read_thread_treats_invalid_app_server_thread_id_as_missing() -> None:
    client = FakeSuperAgentsClient(
        {
            "read_thread": [
                RuntimeError(
                    '{"code": -32600, "message": '
                    '"invalid thread id: invalid character"}'
                )
            ]
        }
    )

    thread = asyncio.run(_manager(client).get_thread_state("s_stale"))

    assert thread is None
    assert client.calls == [
        (
            "read_thread",
            {"thread_id": "s_stale", "include_turns": True},
        )
    ]


def test_read_thread_truncates_completed_turn_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setenv("OPENBASE_CODER_THREAD_HISTORY_LIMIT", "2")
    client = FakeSuperAgentsClient(
        {
            "read_thread": [
                {
                    "thread": _thread(
                        "thr-1",
                        str(project_dir),
                        turns=[
                            _turn(
                                f"turn-{index}",
                                message=f"Prompt {index}",
                                output=f"Output {index}",
                                started_at=index,
                                completed_at=index + 1,
                                status="completed",
                            )
                            for index in range(1, 5)
                        ],
                    )
                }
            ]
        }
    )

    thread = asyncio.run(_manager(client).get_thread_state("thr-1"))

    assert thread is not None
    assert [turn.run_id for turn in thread.run_history] == ["turn-3", "turn-4"]


def test_read_thread_falls_back_to_tracked_summaries_when_payload_is_too_large(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    tracked_turn = SimpleNamespace(
        turn_id="turn-1",
        status="running",
        started_at="2026-05-26T20:10:00.000Z",
        updated_at="2026-05-26T20:11:00.000Z",
        finished_at=None,
        prompt_preview="Inspect repo",
        last_useful_message="Working",
        reasoning_effort="low",
    )
    client = FakeSuperAgentsClient(
        {
            "read_thread": [
                RuntimeError(
                    "sent 1009 (message too big) frame with 1593929 bytes exceeds limit of 1048576 bytes"
                ),
                {"thread": _thread("thr-1", str(project_dir), status="active")},
            ],
            "get_session": [
                SimpleNamespace(turns={"turn-1": tracked_turn}),
            ],
        }
    )

    thread = asyncio.run(_manager(client).get_thread_state("thr-1"))

    assert thread is not None
    assert thread.current_run is not None
    assert thread.current_run.run_id == "turn-1"
    assert thread.current_run.reasoning_effort == "low"
    assert thread.current_run.message == "Inspect repo"
    assert thread.current_run.accumulated_output == "Working"
    assert client.calls[:3] == [
        ("read_thread", {"thread_id": "thr-1", "include_turns": True}),
        ("read_thread", {"thread_id": "thr-1", "include_turns": False}),
        ("get_session", {"thread_id": "thr-1"}),
    ]


def test_read_thread_uses_tracked_super_agents_reasoning_effort(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeSuperAgentsClient(
        {
            "read_thread": [
                {
                    "thread": _thread(
                        "thr-1",
                        str(project_dir),
                        status="active",
                        turns=[
                            _turn(
                                "turn-1",
                                message="Inspect repo",
                                output="Working",
                                started_at=10,
                                completed_at=None,
                                status="inProgress",
                            )
                        ],
                    )
                }
            ],
            "get_session": [
                SimpleNamespace(
                    turns={
                        "turn-1": SimpleNamespace(reasoning_effort="low"),
                    }
                )
            ],
        }
    )

    thread = asyncio.run(_manager(client).get_thread_state("thr-1"))

    assert thread is not None
    assert thread.current_run is not None
    assert thread.current_run.reasoning_effort == "low"
    assert thread.model_dump(mode="json")["current_turn"]["reasoning_effort"] == "low"


def test_read_thread_marks_waiting_turn_as_waiting(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    waiting_status = {"type": "active", "activeFlags": ["waitingOnUserInput"]}
    client = FakeSuperAgentsClient(
        {
            "read_thread": [
                {
                    "thread": _thread(
                        "thr-1",
                        str(project_dir),
                        status=waiting_status,
                        turns=[
                            _turn(
                                "turn-1",
                                message="Need input",
                                output="Pick an option",
                                started_at=10,
                                completed_at=None,
                                status="inProgress",
                            )
                        ],
                    )
                }
            ]
        }
    )

    thread = asyncio.run(_manager(client).get_thread_state("thr-1"))

    assert thread is not None
    assert thread.status == "waiting"
    assert thread.current_run is not None
    assert thread.current_run.status == "waiting"


def test_create_session_reuses_existing_thread_for_directory(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeSuperAgentsClient(
        {"list_threads": [{"data": [_thread("thr-existing", str(project_dir))]}]}
    )

    thread = asyncio.run(_manager(client).create_session(str(project_dir)))

    assert thread.session_id == "thr-existing"
    assert client.calls == [
        (
            "list_threads",
            {
                "use_state_db_only": True,
                "search_term": None,
                "cwd": str(project_dir.resolve()),
                "limit": 1,
                "cursor": None,
            },
        )
    ]


def test_create_thread_starts_new_thread_even_when_directory_has_existing_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setenv(
        "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH",
        str(tmp_path / "MISSING_SUPER_AGENT_INSTRUCTIONS.md"),
    )
    client = FakeSuperAgentsClient(
        {
            "list_threads": [{"data": [_thread("thr-existing", str(project_dir))]}],
            "start_thread": [{"thread": _thread("thr-new", str(project_dir))}],
        }
    )

    thread = asyncio.run(_manager(client).create_thread(str(project_dir)))

    assert thread.session_id == "thr-new"
    assert client.calls == [
        (
            "start_thread",
            {
                "cwd": str(project_dir.resolve()),
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
            },
        )
    ]


def test_create_thread_starts_new_thread_when_none_exist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setenv(
        "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH",
        str(tmp_path / "MISSING_SUPER_AGENT_INSTRUCTIONS.md"),
    )
    client = FakeSuperAgentsClient(
        {
            "start_thread": [{"thread": _thread("thr-new", str(project_dir))}],
        }
    )

    thread = asyncio.run(_manager(client).create_thread(str(project_dir)))

    assert thread.session_id == "thr-new"
    assert client.calls == [
        (
            "start_thread",
            {
                "cwd": str(project_dir.resolve()),
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
            },
        )
    ]


def test_create_thread_includes_super_agent_instructions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    instructions_path = tmp_path / "SUPER_AGENT_INSTRUCTIONS.md"
    instructions_path.write_text("super agent instructions\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", str(instructions_path))
    client = FakeSuperAgentsClient(
        {
            "start_thread": [{"thread": _thread("thr-new", str(project_dir))}],
        }
    )

    thread = asyncio.run(_manager(client).create_thread(str(project_dir)))

    assert thread.session_id == "thr-new"
    assert client.calls == [
        (
            "start_thread",
            {
                "cwd": str(project_dir.resolve()),
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "developerInstructions": "super agent instructions",
            },
        )
    ]


def test_start_turn_starts_via_super_agents_and_broadcasts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    events: list[tuple[str, dict[str, Any]]] = []

    async def fake_broadcast(session_id: str, event: dict[str, Any]) -> None:
        events.append((session_id, event))

    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.session_manager._broadcast",
        fake_broadcast,
    )

    async def fail_announce(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("turn start should not auto-announce Super Agents")

    monkeypatch.setattr(
        session_manager_module,
        "announce_super_agent_start",
        fail_announce,
        raising=False,
    )
    client = FakeSuperAgentsClient(
        {
            "read_thread": [
                {"thread": _thread("thr-1", str(project_dir), status="idle")},
            ],
            "start_turn": [{"turn": {"id": "turn-1", "startedAt": 10}}],
        }
    )

    turn_id = asyncio.run(_manager(client).start_turn("thr-1", "Inspect repo"))

    assert turn_id == "turn-1"
    assert client.calls[-1] == (
        "start_turn",
        {
            "threadId": "thr-1",
            "cwd": str(project_dir),
            "prompt": "Inspect repo",
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        },
    )
    assert events[0][0] == "thr-1"
    assert events[0][1]["type"] == "turn_started"


def test_queue_turn_uses_super_agents_queue_without_exposing_active_turn(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeSuperAgentsClient(
        {
            "read_thread": [
                {
                    "thread": _thread(
                        "thr-1",
                        str(project_dir),
                        status="active",
                        turns=[
                            _turn(
                                "turn-1",
                                message="Inspect",
                                output="Working",
                                started_at=10,
                                completed_at=None,
                                status="inProgress",
                            )
                        ],
                    )
                }
            ],
            "queue_turn_by_label": [
                {
                    "queued": True,
                    "threadId": "thr-1",
                    "queueDepth": 1,
                    "item": {"id": "q_1", "status": "queued"},
                }
            ],
        }
    )
    # queue_turn re-reads state to broadcast the refreshed thread snapshot.
    client.responses["read_thread"].append(client.responses["read_thread"][0])

    manager = CodexAppServerSessionManager(
        client=client,
        model_for_role=lambda role: (
            "gpt-dispatcher" if role == "dispatcher" else "gpt-super-agent"
        ),
    )
    result = asyncio.run(manager.queue_turn("thr-1", "Follow up"))

    assert result["queued"] is True
    assert (
        "queue_turn_by_label",
        {
            "thread_id": "thr-1",
            "cwd": str(project_dir),
            "turn_input": {
                "prompt": "Follow up",
                "cwd": str(project_dir),
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "model": "gpt-super-agent",
            },
        },
    ) in client.calls


def test_steer_turn_sends_prompt_to_active_super_agents_turn(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    active_thread = {
        "thread": _thread(
            "thr-1",
            str(project_dir),
            status="active",
            turns=[
                _turn(
                    "turn-1",
                    message="Inspect",
                    output="Working",
                    started_at=10,
                    completed_at=None,
                    status="inProgress",
                )
            ],
        )
    }
    client = FakeSuperAgentsClient(
        {
            # Two reads resolve the thread and active turn; the third backs
            # the post-steer thread_state broadcast, and the fourth the final
            # get_session_state assertion below.
            "read_thread": [active_thread, active_thread, active_thread, active_thread],
            "steer_by_label": [{"turnId": "turn-1"}],
        }
    )

    manager = _manager(client)

    async def steer_then_read() -> tuple[dict[str, Any], Any]:
        result = await manager.steer_turn("thr-1", "Use the tests")
        return result, await manager.get_session_state("thr-1")

    result, session = asyncio.run(steer_then_read())

    assert result["steered"] is True
    assert result["turn_id"] == "turn-1"
    assert (
        "steer_by_label",
        {
            "thread_id": "thr-1",
            "cwd": str(project_dir),
            "turn_id": "turn-1",
            "prefer": "latest_active",
            "prompt": "Use the tests",
            "turn_input": {"cwd": str(project_dir)},
        },
    ) in client.calls

    # The steer is overlaid on the active turn until the app-server exposes
    # it as a userMessage item.
    assert session is not None and session.current_run is not None
    assert [steer.text for steer in session.current_run.steers] == ["Use the tests"]


def test_turn_prompt_and_steers_derive_from_user_message_items(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    turn = _turn(
        "turn-1",
        message="Original prompt",
        output="Working",
        started_at=10,
        completed_at=None,
        status="inProgress",
    )
    turn["items"].append(
        {
            "type": "userMessage",
            "id": "item-steer",
            "content": [{"type": "text", "text": "Also update docs"}],
        }
    )
    client = FakeSuperAgentsClient(
        {
            "read_thread": [
                {
                    "thread": _thread(
                        "thr-1", str(project_dir), status="active", turns=[turn]
                    )
                }
            ]
        }
    )

    session = asyncio.run(_manager(client).get_session_state("thr-1"))

    assert session is not None and session.current_run is not None
    assert session.current_run.message == "Original prompt"
    assert [steer.text for steer in session.current_run.steers] == ["Also update docs"]
    payload = session.model_dump(mode="json")
    assert payload["current_turn"]["prompt"] == "Original prompt"
    assert payload["current_turn"]["steers"][0]["text"] == "Also update docs"


def test_session_state_includes_queued_turns(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    class QueueClient(FakeSuperAgentsClient):
        def queued_turn_summary(self) -> list[dict[str, Any]]:
            return [
                {
                    "threadId": "thr-1",
                    "queueDepth": 1,
                    "items": [
                        {
                            "id": "q_1",
                            "status": "queued",
                            "queuedAt": "2026-07-10T00:00:00Z",
                            "inputData": {"prompt": "Follow up work"},
                        }
                    ],
                },
                {
                    "threadId": "thr-2",
                    "items": [{"id": "q_2", "inputData": {"prompt": "Other thread"}}],
                },
            ]

    client = QueueClient(
        {
            "read_thread": [
                {
                    "thread": _thread(
                        "thr-1",
                        str(project_dir),
                        status="active",
                        turns=[
                            _turn(
                                "turn-1",
                                message="Inspect",
                                output="Working",
                                started_at=10,
                                completed_at=None,
                                status="inProgress",
                            )
                        ],
                    )
                }
            ]
        }
    )

    session = asyncio.run(_manager(client).get_session_state("thr-1"))

    assert session is not None
    assert [(item.queue_id, item.prompt) for item in session.queued_turns] == [
        ("q_1", "Follow up work")
    ]
    payload = session.model_dump(mode="json")
    assert payload["queued_turns"][0]["prompt"] == "Follow up work"


def test_turn_started_notification_announces_externally_started_turn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    events: list[tuple[str, dict[str, Any]]] = []

    async def fake_broadcast(session_id: str, event: dict[str, Any]) -> None:
        events.append((session_id, event))

    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.session_manager._broadcast",
        fake_broadcast,
    )
    active_thread = {
        "thread": _thread(
            "thr-1",
            str(project_dir),
            status="active",
            turns=[
                _turn(
                    "turn-2",
                    message="Queued prompt",
                    output="",
                    started_at=20,
                    completed_at=None,
                    status="inProgress",
                )
            ],
        )
    }
    client = FakeSuperAgentsClient({"read_thread": [active_thread]})
    manager = _manager(client)

    asyncio.run(
        manager._handle_client_event(
            "turn/started",
            {"threadId": "thr-1", "turn": {"id": "turn-2", "startedAt": 20}},
        )
    )

    assert [event["type"] for _, event in events] == ["turn_started", "thread_state"]
    assert events[0][1]["data"]["turn_id"] == "turn-2"
    assert events[0][1]["data"]["prompt"] == "Queued prompt"
    assert events[1][1]["data"]["current_turn"]["turn_id"] == "turn-2"


def test_interrupt_turn_uses_active_turn_id(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeSuperAgentsClient(
        {
            "read_thread": [
                {
                    "thread": _thread(
                        "thr-1",
                        str(project_dir),
                        status="active",
                        turns=[
                            _turn(
                                "turn-1",
                                message="Inspect",
                                output="Working",
                                started_at=10,
                                completed_at=None,
                                status="inProgress",
                            )
                        ],
                    )
                }
            ],
            "cancel_turn": [{}],
        }
    )

    success = asyncio.run(_manager(client).interrupt_turn("thr-1"))

    assert success is True
    assert client.calls[-1] == (
        "cancel_turn",
        {"thread_id": "thr-1", "turn_id": "turn-1"},
    )


def test_interrupt_turn_returns_false_without_active_turn(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeSuperAgentsClient(
        {
            "read_thread": [
                {
                    "thread": _thread(
                        "thr-1",
                        str(project_dir),
                        status="idle",
                        turns=[
                            _turn(
                                "turn-old",
                                message="Done",
                                output="Done",
                                started_at=5,
                                completed_at=6,
                                status="completed",
                            )
                        ],
                    )
                }
            ]
        }
    )

    success = asyncio.run(_manager(client).interrupt_turn("thr-1"))

    assert success is False
    assert all(call[0] != "cancel_turn" for call in client.calls)


def test_interrupt_turn_ignores_stale_local_turn_mapping(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeSuperAgentsClient(
        {
            "read_thread": [
                {"thread": _thread("thr-1", str(project_dir), status="idle")},
                {
                    "thread": _thread(
                        "thr-1",
                        str(project_dir),
                        status="idle",
                        turns=[
                            _turn(
                                "turn-1",
                                message="Done",
                                output="Done",
                                started_at=5,
                                completed_at=6,
                                status="completed",
                            )
                        ],
                    )
                },
            ],
            "start_turn": [{"turn": {"id": "turn-1", "startedAt": 10}}],
        }
    )
    manager = _manager(client)

    turn_id = asyncio.run(manager.start_turn("thr-1", "Inspect repo"))
    success = asyncio.run(manager.interrupt_turn("thr-1"))

    assert turn_id == "turn-1"
    assert success is False
    assert all(call[0] != "cancel_turn" for call in client.calls)


def test_resume_thread_with_developer_instructions_uses_thread_resume(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = FakeSuperAgentsClient({"request:thread/resume": [{}]})

    asyncio.run(
        _manager(client).resume_thread_with_developer_instructions(
            "thr-1",
            str(project_dir),
            "direct LiveKit instructions",
        )
    )

    assert client.calls[0] == ("ensure_connected", {})
    assert client.calls[1][0] == "request"
    assert client.calls[1][1]["method"] == "thread/resume"
    assert client.calls[1][1]["params"]["threadId"] == "thr-1"
    assert client.calls[1][1]["params"]["cwd"] == str(project_dir)
    assert client.calls[1][1]["params"]["developerInstructions"] == (
        "direct LiveKit instructions"
    )


def test_resume_thread_without_explicit_instructions_uses_super_agent_instructions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    instructions_path = tmp_path / "SUPER_AGENT_INSTRUCTIONS.md"
    instructions_path.write_text("super resume instructions\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", str(instructions_path))
    client = FakeSuperAgentsClient({"request:thread/resume": [{}]})

    asyncio.run(_manager(client)._resume_thread("thr-1", str(project_dir)))

    assert client.calls[1][0] == "request"
    assert client.calls[1][1]["method"] == "thread/resume"
    assert client.calls[1][1]["params"]["developerInstructions"] == (
        "super resume instructions"
    )


def test_turn_failed_broadcasts_error_envelope(monkeypatch, tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    events: list[tuple[str, dict[str, Any]]] = []

    async def fake_broadcast(session_id: str, event: dict[str, Any]) -> None:
        events.append((session_id, event))

    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.session_manager._broadcast",
        fake_broadcast,
    )
    client = FakeSuperAgentsClient(
        {
            "read_thread": [
                {"thread": _thread("thr-1", str(project_dir), status="idle")},
            ],
        }
    )

    asyncio.run(
        _manager(client)._handle_client_event(
            "turn/failed",
            {
                "threadId": "thr-1",
                "turnId": "turn-1",
                "error": {"message": "Codex turn exploded"},
            },
        )
    )

    error_events = [event for _sid, event in events if event["type"] == "error"]
    assert error_events == [
        {
            "type": "error",
            "data": {
                "message": "Codex turn exploded",
                "code": "turn_failed",
                "turn_id": "turn-1",
            },
        }
    ]
    assert [event["type"] for _sid, event in events].count("turn_completed") == 1


def test_agent_message_item_boundaries_are_preserved_in_live_output(
    monkeypatch,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    async def fake_broadcast(session_id: str, event: dict[str, Any]) -> None:
        events.append((session_id, event))

    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.session_manager._broadcast",
        fake_broadcast,
    )
    manager = _manager(FakeSuperAgentsClient({}))

    async def check() -> None:
        await manager._handle_client_event(
            "item/agentMessage/delta",
            {
                "threadId": "thr-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "delta": "plus console.",
            },
        )
        await manager._handle_client_event(
            "item/completed",
            {
                "threadId": "thr-1",
                "turnId": "turn-1",
                "item": {
                    "id": "item-1",
                    "type": "agentMessage",
                    "text": "plus console.",
                },
            },
        )
        await manager._handle_client_event(
            "item/agentMessage/delta",
            {
                "threadId": "thr-1",
                "turnId": "turn-1",
                "itemId": "item-2",
                "delta": "The new agent is Cindy. ",
            },
        )
        await manager._handle_client_event(
            "item/completed",
            {
                "threadId": "thr-1",
                "turnId": "turn-1",
                "item": {
                    "id": "item-2",
                    "type": "agentMessage",
                    "text": "The new agent is Cindy. I'm starting her now.",
                },
            },
        )

    asyncio.run(check())

    output_lines = [
        event["data"]["line"]
        for _session_id, event in events
        if event["type"] == "output_update"
    ]
    assert output_lines == [
        "plus console.",
        "\n\nThe new agent is Cindy. ",
        "I'm starting her now.",
    ]


def test_trigger_and_event_methods_delegate_to_super_agents_client() -> None:
    client = FakeSuperAgentsClient(
        {
            "add_routine_trigger": [{"trigger": {"id": "trg-1"}}],
            "remove_routine_trigger": [{"deleted": True}],
            "deliver_webhook_event": [{"status": "delivered"}],
            "emit_routine_event": [{"status": "delivered"}],
        }
    )
    manager = _manager(client)

    assert asyncio.run(
        manager.add_routine_trigger("daily", {"senderPath": "sender.id"})
    ) == {"trigger": {"id": "trg-1"}}
    assert asyncio.run(manager.remove_routine_trigger("daily", "trg-1")) == {
        "deleted": True
    }
    assert asyncio.run(manager.deliver_webhook_event("tok", body=b"{}")) == {
        "status": "delivered"
    }
    assert asyncio.run(manager.emit_routine_event("daily", {"a": 1}, "evt-1")) == {
        "status": "delivered"
    }
    assert client.calls[-4:] == [
        ("add_routine_trigger", {"name": "daily", "senderPath": "sender.id"}),
        ("remove_routine_trigger", {"name": "daily", "triggerId": "trg-1"}),
        ("deliver_webhook_event", {"token": "tok", "origin": "external"}),
        (
            "emit_routine_event",
            {"name": "daily", "payload": {"a": 1}, "eventId": "evt-1"},
        ),
    ]
