"""Manual chat titles across the real Claude store and mobile API snapshots."""

import asyncio
from pathlib import Path

import pytest
from super_agents.agent_store import Store
from super_agents.claude_sdk import ClaudeAgentSdkClient

from openbase_coder_cli.openbase_coder_cli_app.thread_metadata import (
    annotate_thread_payload,
)
from openbase_coder_cli.thread_sync.session_manager import CodexAppServerSessionManager


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["claude_code", "openbase_cloud"])
async def test_four_manual_threads_take_their_first_message_as_title(
    tmp_path: Path, monkeypatch, backend: str
):
    monkeypatch.setenv("SUPER_AGENTS_CLAUDE_CODE_HOME", str(tmp_path / "agents"))
    monkeypatch.setenv(
        "SUPER_AGENTS_DEFAULT_CONFIG_PATH", str(tmp_path / "config.json")
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("SUPER_AGENTS_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv(
        "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", str(tmp_path / "instructions.md")
    )
    project = tmp_path / "tic-tac-toe"
    project.mkdir()
    store = Store(tmp_path / "sessions.sqlite3")
    client = ClaudeAgentSdkClient(store=store, backend_identity=backend)
    # Exercise the real turn acceptance/persistence path without any model,
    # external process, polling service, or network call.
    monkeypatch.setattr(client, "_require_sdk", lambda: object())
    monkeypatch.setattr(client, "_spawn_turn_task", lambda *args, **kwargs: None)
    manager = CodexAppServerSessionManager(client=client, model_for_role=lambda _: None)
    monkeypatch.setattr(manager, "_watch_legacy_claude_thread", lambda _: None)
    manager.BACKEND_SESSIONS_SHARE_SECONDS = 0

    # A reusable agent label in the same directory must remain untouched.
    named = await client.start_thread({"name": "tic-tac-toe", "cwd": str(project)})
    threads = await asyncio.gather(
        *(manager.create_thread(str(project)) for _ in range(4))
    )
    # Titles are display-only and never carry the thread id; the unique
    # lookup name stays internal.
    assert [thread.name for thread in threads] == ["tic-tac-toe"] * 4
    assert store.get_session(named["threadId"]).name == "tic-tac-toe"

    prompts = [
        "what is 17 times 23",
        "fix the board",
        "add a reset button",
        "what is 17 times 23",
    ]
    for thread, prompt in zip(threads, prompts, strict=True):
        turn_id = await manager.start_turn(thread.session_id, prompt)
        # A failed first turn must still have its task title.
        store.update_turn(turn_id, status="error")
        store.update_session(thread.session_id, status="error", active_turn_id=None)

    displayed = []
    for thread, prompt in zip(threads, prompts, strict=True):
        state = await manager.get_thread_state(thread.session_id)
        payload = annotate_thread_payload(state.model_dump(mode="json"))
        assert payload["display_name"] == payload["name"] == payload["title"] == prompt
        assert thread.session_id[-8:] not in payload["display_name"]
        displayed.append(payload["display_name"])
    listed = {thread.session_id: thread.name for thread in await manager.list_threads()}
    assert [listed[thread.session_id] for thread in threads] == displayed

    first = threads[0]
    await manager.start_turn(first.session_id, "now do something else")
    assert (await manager.get_thread_state(first.session_id)).name == displayed[0]
    await manager.rename_thread(first.session_id, "My arithmetic question")
    assert (
        await manager.get_thread_state(first.session_id)
    ).name == "My arithmetic question"
    # API names are display titles; internal lookup names stay stable.
    assert store.get_session(first.session_id).name.startswith("thread-")
    await client.close()
