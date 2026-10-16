"""Super Agent turns started from a voice call are owned by django-cli.

The LiveKit job process exits when the call's room closes; a turn it owned
died with it (Maritime 28, 2026-10-16). The job's super_agents MCP server
therefore forwards every operation to django-cli.
"""

from __future__ import annotations

# ruff: noqa: E402, I001
import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django
import httpx
import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

django.setup()

from super_agents.app_models import LabelQueryInput, QueueCancelInput

from openbase_coder_cli import super_agents_remote as remote
from openbase_coder_cli.openbase_coder_cli_app import super_agents_invoke as view_module
from openbase_coder_cli.super_agents_remote import (
    REMOTE_METHODS,
    RemoteSuperAgentsClient,
    RemoteSuperAgentsError,
    RemoteSuperAgentsUnavailable,
    decode_argument,
    encode_argument,
    invoke_super_agents_method,
)


class LocalClient:
    backend = "claude_code"
    store = object()

    def __init__(self):
        self.calls = []

    def _resolve_session(self, query):
        return SimpleNamespace(id=query.thread_id, active_turn_id=None)

    async def start_turn_by_label(self, query, turn_input):
        self.calls.append(("start_turn_by_label", query, turn_input))
        return {"turnId": "t_1", "threadId": query.thread_id}

    async def resume_by_label(
        self,
        query,
        *,
        developer_instructions=None,
        replace_developer_instructions=False,
    ):
        self.calls.append(
            (
                "resume_by_label",
                query,
                developer_instructions,
                replace_developer_instructions,
            )
        )
        return {"threadId": query.thread_id}

    async def steer_by_label(self, query, prompt, turn_input=None):
        raise ValueError("Expected active turn id `x` but found `y`.")

    async def cancel_queued_turn(self, query):
        self.calls.append(("cancel_queued_turn", query))
        return {"cancelled": True}


def test_dataclass_arguments_round_trip_through_the_wire():
    query = LabelQueryInput(
        label="Casper", thread_id="s_1", prefer="latest_any", turn_id=None
    )
    cancel = QueueCancelInput(queue_item_id="t_9", position=2)
    encoded = encode_argument([query, {"nested": cancel, "text": "x"}, 3])
    assert encoded[0]["__super_agents_type__"] == "LabelQueryInput"
    assert "turn_id" not in encoded[0]["fields"]
    decoded = decode_argument(encoded)
    assert decoded == [query, {"nested": cancel, "text": "x"}, 3]


def test_unknown_argument_type_is_rejected():
    with pytest.raises(ValueError):
        decode_argument({"__super_agents_type__": "Session", "fields": {}})


def test_remote_methods_cover_every_mcp_tool_operation():
    for name in (
        "start_thread",
        "start_turn_by_label",
        "queue_turn_by_label",
        "steer_by_label",
        "cancel_by_label",
        "answer_request",
        "resume_by_label",
        "progress_by_label",
    ):
        assert name in REMOTE_METHODS
    assert "_resolve_session" not in REMOTE_METHODS
    assert "close" not in REMOTE_METHODS


async def test_invoke_runs_the_named_method_with_decoded_arguments():
    local = LocalClient()
    payload = remote.encode_request(
        "claude_code",
        "resume_by_label",
        (LabelQueryInput(thread_id="s_1"),),
        {"developer_instructions": "Be brief.", "replace_developer_instructions": True},
    )
    result = await invoke_super_agents_method(local, payload)
    assert result == {"threadId": "s_1"}
    assert local.calls == [
        ("resume_by_label", LabelQueryInput(thread_id="s_1"), "Be brief.", True)
    ]


async def test_invoke_rejects_unknown_methods_and_other_backends():
    local = LocalClient()
    with pytest.raises(ValueError):
        await invoke_super_agents_method(
            local, {"method": "_resolve_session", "args": []}
        )
    with pytest.raises(ValueError):
        await invoke_super_agents_method(local, {"method": "close", "args": []})
    with pytest.raises(ValueError):
        await invoke_super_agents_method(
            local, {"backend": "codex", "method": "start_turn_by_label", "args": []}
        )
    assert local.calls == []


def _transport_to(local: LocalClient, seen: list):
    async def handle(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(request)
        payload = json.loads(request.content)
        try:
            result = await invoke_super_agents_method(local, payload)
        except ValueError as exc:
            return httpx.Response(400, json={"error": str(exc), "type": "ValueError"})
        return httpx.Response(200, json={"result": result})

    return httpx.MockTransport(handle)


async def test_remote_client_forwards_operations_and_keeps_local_lookups():
    local = LocalClient()
    seen = []
    client = RemoteSuperAgentsClient(
        local, base_url="http://127.0.0.1:1", transport=_transport_to(local, seen)
    )
    assert client.backend == "claude_code"
    assert client.store is local.store
    assert client._resolve_session(LabelQueryInput(thread_id="s_2")).id == "s_2"

    result = await client.start_turn_by_label(
        LabelQueryInput(thread_id="s_2"), {"prompt": "Build it.", "cwd": "/tmp"}
    )
    assert result == {"turnId": "t_1", "threadId": "s_2"}
    assert local.calls == [
        (
            "start_turn_by_label",
            LabelQueryInput(thread_id="s_2"),
            {"prompt": "Build it.", "cwd": "/tmp"},
        )
    ]
    assert seen[0].url.path == remote.INVOKE_PATH
    assert seen[0].headers["authorization"].startswith("Bearer ")

    cancelled = await client.cancel_queued_turn(QueueCancelInput(queue_item_id="t_9"))
    assert cancelled == {"cancelled": True}
    assert local.calls[-1] == (
        "cancel_queued_turn",
        QueueCancelInput(queue_item_id="t_9"),
    )

    with pytest.raises(ValueError, match="Expected active turn id"):
        await client.steer_by_label(LabelQueryInput(thread_id="s_2"), "More.")
    with pytest.raises(AttributeError):
        client.close  # noqa: B018 - lifecycle stays with the owning process


async def test_remote_client_reports_an_unreachable_api_without_running_anything():
    local = LocalClient()

    async def refuse(request):
        raise httpx.ConnectError("connection refused")

    client = RemoteSuperAgentsClient(
        local, base_url="http://127.0.0.1:1", transport=httpx.MockTransport(refuse)
    )
    with pytest.raises(RemoteSuperAgentsUnavailable):
        await client.start_turn_by_label(
            LabelQueryInput(thread_id="s_3"), {"prompt": "x"}
        )
    assert local.calls == []

    async def fail(request):
        return httpx.Response(
            400,
            json={"error": "Workspace busy", "code": "busy", "type": "WorkspaceBusy"},
        )

    client = RemoteSuperAgentsClient(
        local, base_url="http://127.0.0.1:1", transport=httpx.MockTransport(fail)
    )
    with pytest.raises(RemoteSuperAgentsError, match="Workspace busy") as info:
        await client.start_turn_by_label(
            LabelQueryInput(thread_id="s_3"), {"prompt": "x"}
        )
    assert info.value.code == "busy"


class FakeManager:
    def __init__(self, client):
        self.client = client
        self.requested_backends = []

    def super_agents_client_for_backend(self, backend):
        self.requested_backends.append(backend)
        if backend and backend != self.client.backend:
            raise ValueError("backend mismatch")
        return self.client


def _post(payload):
    request = APIRequestFactory().post(
        "/api/super-agents/invoke/", payload, format="json"
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return view_module.super_agents_invoke(request)


def test_view_runs_the_operation_on_this_process_client(monkeypatch):
    local = LocalClient()
    manager = FakeManager(local)
    monkeypatch.setattr(view_module, "get_session_manager", lambda: manager)
    invalidated = []
    monkeypatch.setattr(
        view_module, "invalidate_thread_list_cache", lambda: invalidated.append(True)
    )

    response = _post(
        remote.encode_request(
            "claude_code",
            "start_turn_by_label",
            (LabelQueryInput(thread_id="s_4"),),
            {"turn_input": {"prompt": "Go."}},
        )
    )
    assert response.status_code == 200
    assert response.data == {"result": {"turnId": "t_1", "threadId": "s_4"}}
    assert local.calls[0][1] == LabelQueryInput(thread_id="s_4")
    assert manager.requested_backends == ["claude_code"]
    assert invalidated == [True]


def test_view_reports_failures_with_their_type(monkeypatch):
    local = LocalClient()
    monkeypatch.setattr(view_module, "get_session_manager", lambda: FakeManager(local))
    response = _post(
        remote.encode_request(
            "claude_code",
            "steer_by_label",
            (LabelQueryInput(thread_id="s_4"), "More."),
            {},
        )
    )
    assert response.status_code == 400
    assert response.data["type"] == "ValueError"
    assert "Expected active turn id" in response.data["error"]

    response = _post({"backend": "codex", "method": "start_turn_by_label", "args": []})
    assert response.status_code == 400
    assert response.data["type"] == "ValueError"


def test_voice_job_binds_delegation_to_the_forwarding_client(monkeypatch, tmp_path):
    from super_agents.agent_store import Store

    from openbase_coder_cli.agent_announcements import claude as managed
    from openbase_coder_cli.livekit_agent.super_agents_client_threads import (
        VOICE_DELEGATION_IN_PROCESS_ENV,
        voice_delegation_client_factory,
    )

    monkeypatch.delenv(VOICE_DELEGATION_IN_PROCESS_ENV, raising=False)
    factory = voice_delegation_client_factory()
    assert factory is RemoteSuperAgentsClient
    monkeypatch.setenv(VOICE_DELEGATION_IN_PROCESS_ENV, "1")
    assert voice_delegation_client_factory() is None

    captured = {}

    def configure_delegation(options, client, ledger, parent_id):
        captured["client"] = client

    monkeypatch.setattr(managed, "configure_delegation", configure_delegation)
    monkeypatch.setattr(managed, "get_data_dir", lambda: tmp_path)
    store = Store(tmp_path / "agents.sqlite3", backend="claude_code")
    client = managed.managed_claude_client(
        store=store, backend_identity="claude_code", delegation_client_factory=factory
    )
    session = store.create_session("worker-a", cwd=str(tmp_path))
    options = SimpleNamespace(mcp_servers={}, hooks={}, system_prompt=None)
    managed_announcements = _announcements_for(client)
    managed_announcements.configure_session(session, options, SimpleNamespace())
    assert isinstance(captured["client"], RemoteSuperAgentsClient)
    assert captured["client"]._local is client

    plain = managed.managed_claude_client(store=store, backend_identity="claude_code")
    _announcements_for(plain).configure_session(session, options, SimpleNamespace())
    assert captured["client"] is plain


def _announcements_for(client):
    # The managed client holds its announcements through the configure hook.
    return client._configure_session.__self__
