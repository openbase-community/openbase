from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from openbase_coder_cli.thread_sync import continuation_store as store
from openbase_coder_cli.thread_sync import continuations as service
from openbase_coder_cli.thread_sync.continuation_context import (
    build_context,
    combine_messages,
)
from openbase_coder_cli.thread_sync.models import ThreadInfo, ThreadStatus


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "SUPER_AGENTS_INITIAL_CONTEXT_DB", str(tmp_path / "initial.sqlite3")
    )
    monkeypatch.setattr(
        service, "load_super_agent_developer_instructions", lambda: None
    )
    monkeypatch.setattr(service, "readiness", AsyncMock(return_value=None))
    monkeypatch.setattr(
        service,
        "get_livekit_voice_route_state",
        lambda: SimpleNamespace(
            dispatcher_thread_id=None, active_target_thread_id=None
        ),
    )
    now = datetime.now(UTC)
    source = ThreadInfo(
        session_id="source",
        directory=str(tmp_path),
        name="Fix login",
        backend="codex",
        updated_at=now,
    )
    history = AsyncMock(
        return_value=[{"role": "user", "text": "Keep the lighthouse blue"}]
    )
    monkeypatch.setattr(service, "read_session_messages", history)
    clients = {
        backend: SimpleNamespace(
            backend=backend,
            ensure_connected=AsyncMock(),
            request=AsyncMock(return_value={}),
            rename_by_label=AsyncMock(),
            set_thread_name=AsyncMock(),
            start_thread=AsyncMock(return_value={"threadId": f"new-{backend}"}),
            store=SimpleNamespace(get_by_name=lambda _: None),
        )
        for backend in service.BACKENDS
    }
    managers = {
        backend: SimpleNamespace(_client=client, _codex_permission_defaults=lambda: {})
        for backend, client in clients.items()
    }
    manager = SimpleNamespace(
        get_thread_state=AsyncMock(return_value=source),
        manager_for_backend=managers.get,
        list_approval_requests=AsyncMock(return_value=[]),
    )
    return manager, source, clients, history


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_backend,target", [("codex", "claude_code"), ("claude_code", "codex")]
)
async def test_switch_both_directions_preserves_source_and_retries(
    setup, source_backend, target
):
    manager, source, clients, _ = setup
    source.backend = source_backend
    request_id = str(uuid4())
    result = await service.create_continuation(
        manager, source.session_id, target, request_id
    )
    assert result["name"] == f"Fix login · {service.BACKENDS[target]}"
    assert source.name == "Fix login"
    assert result == await service.create_continuation(
        manager, source.session_id, target, request_id
    )
    assert clients[target].start_thread.await_count == 1
    if target == "codex":
        clients[target].set_thread_name.assert_awaited_once_with(
            result["thread_id"], result["name"]
        )
        clients[target].rename_by_label.assert_not_called()
    record = store.for_destination(result["thread_id"])
    assert record["messages"][0]["text"] == "Keep the lighthouse blue"
    assert store.links("source")["continuations"][0]["thread_id"] == result["thread_id"]
    assert store.links(result["thread_id"])["continued_from"]["thread_id"] == "source"


@pytest.mark.asyncio
async def test_round_trip_replaces_suffix_and_keeps_inherited_context(setup):
    manager, source, _, history = setup
    first = await service.create_continuation(
        manager, "source", "claude_code", str(uuid4())
    )
    source.session_id = first["thread_id"]
    source.backend = "claude_code"
    source.name = first["name"]
    history.return_value = [{"role": "user", "text": "Also keep the door red"}]
    second = await service.create_continuation(
        manager, source.session_id, "codex", str(uuid4())
    )
    assert second["name"] == "Fix login · Codex"
    assert len(store.for_destination(second["thread_id"])["messages"]) == 2


@pytest.mark.asyncio
async def test_busy_source_cannot_create_destination(setup):
    manager, source, clients, _ = setup
    source.raw_status = ThreadStatus.running
    with pytest.raises(ValueError, match="Finish or stop"):
        await service.create_continuation(
            manager, "source", "claude_code", str(uuid4())
        )
    clients["claude_code"].start_thread.assert_not_called()


@pytest.mark.asyncio
async def test_source_change_during_export_cannot_create_destination(setup):
    manager, source, clients, _ = setup
    changed = source.model_copy(update={"updated_at": datetime.now(UTC)})
    manager.get_thread_state.side_effect = [source, changed]
    with pytest.raises(ValueError, match="changed"):
        await service.create_continuation(
            manager, "source", "claude_code", str(uuid4())
        )
    clients["claude_code"].start_thread.assert_not_called()


@pytest.mark.asyncio
async def test_creation_timeout_is_never_repeated(setup):
    manager, _, clients, _ = setup
    clients["claude_code"].start_thread.side_effect = TimeoutError("unknown outcome")
    request_id = str(uuid4())
    with pytest.raises(TimeoutError):
        await service.create_continuation(manager, "source", "claude_code", request_id)
    with pytest.raises(RuntimeError):
        await service.create_continuation(manager, "source", "claude_code", request_id)
    assert clients["claude_code"].start_thread.await_count == 1
    assert store.for_operation(request_id)["safe_to_retry"] is False


def test_large_context_is_bounded_and_discloses_omission():
    messages = [
        {"role": "user", "text": "Important constraint"},
        {"role": "tool", "text": "x" * 100_000},
        {"role": "assistant", "text": "Latest outcome"},
    ]
    context, omitted = build_context(messages, "Example")
    assert omitted
    assert "Important constraint" in context
    assert "Latest outcome" in context
    assert len(context) < 65_000


def test_inherited_context_is_not_recursively_embedded():
    messages = combine_messages(
        [{"role": "user", "text": "old"}],
        [
            {
                "role": "user",
                "text": "[Conversation context native]\nold\n[Current user message]\nnew",
            }
        ],
    )
    assert messages == [
        {"role": "user", "text": "old"},
        {"role": "user", "text": "new"},
    ]


@pytest.mark.asyncio
async def test_renamed_source_uses_new_title(setup):
    manager, source, _, _ = setup
    first = await service.create_continuation(
        manager, "source", "claude_code", str(uuid4())
    )
    source.session_id, source.backend, source.name = (
        first["thread_id"],
        "claude_code",
        "Updated task",
    )
    result = await service.create_continuation(
        manager, source.session_id, "codex", str(uuid4())
    )
    assert result["name"] == "Updated task · Codex"


@pytest.mark.asyncio
async def test_finalize_failure_can_recover_without_recreating(setup, monkeypatch):
    manager, _, clients, _ = setup
    real_register = service.initialize_session_context
    monkeypatch.setattr(
        service,
        "initialize_session_context",
        AsyncMock(side_effect=OSError("disk unavailable")),
    )
    request_id = str(uuid4())
    with pytest.raises(OSError):
        await service.create_continuation(manager, "source", "claude_code", request_id)
    assert store.for_operation(request_id)["state"] == "created"
    monkeypatch.setattr(service, "initialize_session_context", real_register)
    result = await service.create_continuation(
        manager, "source", "claude_code", request_id
    )
    assert result["thread_id"] == "new-claude_code"
    assert clients["claude_code"].start_thread.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["voice", "approval", "queue"])
async def test_active_work_blocks_switch(setup, monkeypatch, blocker):
    from openbase_coder_cli.thread_sync.models import QueuedTurnInfo

    manager, source, clients, _ = setup
    if blocker == "voice":
        monkeypatch.setattr(
            service,
            "get_livekit_voice_route_state",
            lambda: SimpleNamespace(
                dispatcher_thread_id=None, active_target_thread_id="source"
            ),
        )
    elif blocker == "approval":
        manager.list_approval_requests.return_value = [{"thread_id": "source"}]
    else:
        source.queued_turns = [QueuedTurnInfo(prompt="Next")]
    with pytest.raises(ValueError):
        await service.create_continuation(
            manager, "source", "claude_code", str(uuid4())
        )
    clients["claude_code"].start_thread.assert_not_called()


def test_api_validates_payload_and_exposes_context(setup, monkeypatch):
    import django

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")
    monkeypatch.setenv("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
    django.setup()
    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import thread_continuations as views

    manager, _, _, _ = setup
    monkeypatch.setattr(views, "get_session_manager", lambda: manager)
    factory = APIRequestFactory()
    user = SimpleNamespace(is_authenticated=True)
    bad = factory.post(
        "/api/threads/source/continuations/", {"backend": "invalid"}, format="json"
    )
    force_authenticate(bad, user=user)
    assert views.thread_continuations(bad, "source").status_code == 400
    request = factory.post(
        "/api/threads/source/continuations/",
        {"backend": "claude_code", "request_id": str(uuid4())},
        format="json",
    )
    force_authenticate(request, user=user)
    response = views.thread_continuations(request, "source")
    assert response.status_code == 201
    context = factory.get("/api/threads/new-claude_code/continuation-context/")
    force_authenticate(context, user=user)
    response = views.thread_continuation_context(context, "new-claude_code")
    assert response.status_code == 200
    assert response.data["messages"][0]["text"] == "Keep the lighthouse blue"
