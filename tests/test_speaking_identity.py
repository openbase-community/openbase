from unittest.mock import AsyncMock

import pytest

from openbase_coder_cli.livekit_voice_route import super_agent_voice_for_context
from openbase_coder_cli.thread_sync.models import ThreadInfo
from openbase_coder_cli.thread_sync.speaking_identity import ensure_speaking_identity


@pytest.mark.asyncio
async def test_claude_identity_is_available_to_real_roster_and_turn_prompt(
    tmp_path, monkeypatch
):
    from super_agents.agent_store import Store
    from super_agents.claude_sdk import ClaudeAgentSdkClient

    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    store = Store(tmp_path / "sessions.sqlite3")
    client = ClaudeAgentSdkClient(store=store)
    started = await client.start_thread(
        {
            "name": "disposable-project",
            "cwd": str(tmp_path),
            "developerInstructions": "Preserve this project instruction.",
        }
    )
    thread = ThreadInfo(
        session_id=started["threadId"],
        directory=str(tmp_path),
        name="disposable-project",
    )
    expected = super_agent_voice_for_context(thread.session_id, thread.name).name
    previous_update = "2025-01-01T00:00:00Z"
    store.update_session(thread.session_id, updated_at=previous_update)
    await ensure_speaking_identity(client, thread)
    persisted = store.get_session(thread.session_id)
    assert persisted.agent_name == expected
    assert persisted.updated_at == previous_update
    assert "Preserve this project instruction." in persisted.developer_instructions
    prompt = client._prompt_for_session(persisted, {"prompt": "What is your name?"})
    assert expected in prompt
    roster = client._session_view(persisted, None)
    assert roster["agentName"] == expected
    # Display-title changes do not choose a new person.
    renamed = ThreadInfo(
        session_id=thread.session_id, directory=str(tmp_path), name="New project title"
    )
    await ensure_speaking_identity(client, renamed)
    assert renamed.agent_name == expected
    await client.close()


@pytest.mark.asyncio
async def test_codex_identity_persists_and_preserves_existing_assignment(
    tmp_path, monkeypatch
):
    from super_agents.app_server_client import CodexAppServerClient

    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    client = CodexAppServerClient(state_file=tmp_path / "state.json")
    thread = ThreadInfo(session_id="thread", directory=str(tmp_path), name="project")
    expected = super_agent_voice_for_context("thread", "project").name
    await ensure_speaking_identity(client, thread)
    assert (await client.get_session("thread")).agent_name == expected
    await client.merge_session("thread", {"agentName": "Blake"})
    thread.agent_name = None
    await ensure_speaking_identity(client, thread)
    assert thread.agent_name == "Blake"
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list", "page", "read", "reuse"])
async def test_codex_existing_session_identity_migration(
    tmp_path, monkeypatch, operation
):
    from super_agents.app_server_client import CodexAppServerClient

    from openbase_coder_cli.thread_sync.session_manager import (
        CodexAppServerSessionManager,
    )

    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    client = CodexAppServerClient(state_file=tmp_path / "state.json")
    payload = {"id": "existing", "cwd": str(tmp_path), "name": "project"}
    monkeypatch.setattr(
        client,
        "list_threads",
        AsyncMock(return_value={"data": [payload]}),
    )
    monkeypatch.setattr(
        client, "read_thread_page", AsyncMock(return_value={"thread": payload})
    )
    manager = CodexAppServerSessionManager(client=client, execution_backend="codex")
    if operation == "list":
        thread = (await manager.list_sessions())[0]
    elif operation == "page":
        thread = (await manager.list_thread_page(limit=10)).threads[0]
    elif operation == "read":
        thread = await manager.get_session_state("existing")
    else:
        thread = await manager.create_session(str(tmp_path), reuse_existing=True)
    expected = super_agent_voice_for_context("existing", "project").name
    assert thread.agent_name == expected
    assert (await client.get_session("existing")).agent_name == expected
    await client.close()
