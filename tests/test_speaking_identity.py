from types import SimpleNamespace

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
    await ensure_speaking_identity(client, thread)
    persisted = store.get_session(thread.session_id)
    assert persisted.agent_name == expected
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
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    records = {}

    async def get_session(thread_id):
        return records.get(thread_id)

    async def merge_session(thread_id, patch):
        records[thread_id] = SimpleNamespace(agent_name=patch["agentName"])

    client = SimpleNamespace(get_session=get_session, merge_session=merge_session)
    thread = ThreadInfo(session_id="thread", directory=str(tmp_path), name="project")
    expected = super_agent_voice_for_context("thread", "project").name
    await ensure_speaking_identity(client, thread)
    assert records["thread"].agent_name == expected
    records["thread"].agent_name = "Blake"
    thread.agent_name = None
    await ensure_speaking_identity(client, thread)
    assert thread.agent_name == "Blake"
