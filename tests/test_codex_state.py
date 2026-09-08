from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from openbase_coder_cli.thread_sync.codex_state import (
    ThreadSyncSafety,
    _active_super_agent_thread_ids,
    _thread_safe_for_sync,
    state_db_path,
)


def _create_state_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL,
                title TEXT NOT NULL,
                sandbox_policy TEXT NOT NULL,
                approval_mode TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                cli_version TEXT NOT NULL DEFAULT '',
                first_user_message TEXT NOT NULL DEFAULT '',
                model TEXT,
                reasoning_effort TEXT,
                created_at_ms INTEGER,
                updated_at_ms INTEGER,
                preview TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE thread_dynamic_tools (
                thread_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                input_schema TEXT NOT NULL,
                defer_loading INTEGER NOT NULL DEFAULT 0,
                namespace TEXT,
                PRIMARY KEY(thread_id, position)
            )
            """
        )


def _insert_thread(home: Path, thread_id: str, *, title: str, updated_at: int) -> Path:
    rollout_path = (
        home
        / "sessions"
        / "2026"
        / "05"
        / "21"
        / f"rollout-2026-05-21T10-00-00-{thread_id}.jsonl"
    )
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    rollout_path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": thread_id}}) + "\n",
        encoding="utf-8",
    )
    with sqlite3.connect(home / "state_5.sqlite") as conn:
        conn.execute(
            """
            INSERT INTO threads (
                id, rollout_path, created_at, updated_at, source, model_provider,
                cwd, title, sandbox_policy, approval_mode, archived, cli_version,
                first_user_message, model, reasoning_effort, created_at_ms,
                updated_at_ms, preview
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                str(rollout_path),
                updated_at - 5,
                updated_at,
                "cli",
                "openai",
                "/tmp/project",
                title,
                "danger-full-access",
                "never",
                0,
                "0.1.0",
                title,
                "gpt-test",
                "high",
                (updated_at - 5) * 1000,
                updated_at * 1000,
                title,
            ),
        )
    return rollout_path


def _append_terminal(
    rollout_path: Path, turn_id: str = "turn-1", message: str = "done"
) -> None:
    with rollout_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-05-21T12:00:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn_id,
                        "last_agent_message": message,
                    },
                }
            )
            + "\n"
        )


def _create_super_agents_store(db_path: Path, rows: list[tuple]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table sessions (
                id text primary key,
                status text not null,
                active_turn_id text,
                backend_session_id text
            )
            """
        )
        conn.executemany("insert into sessions values (?, ?, ?, ?)", rows)


def test_active_super_agent_thread_ids_reads_sqlite_store(tmp_path: Path) -> None:
    db_path = tmp_path / "store" / "state.sqlite3"
    _create_super_agents_store(
        db_path,
        [
            ("s_running", "running", None, "0197aaaa-1111-2222-3333-444455556666"),
            ("s_turn", "waiting", "turn-1", "0197bbbb-1111-2222-3333-444455556666"),
            ("s_idle", "waiting", None, "0197cccc-1111-2222-3333-444455556666"),
            ("codex_0197dddd111122223333444455556666", "running", None, None),
            ("claude_0197eeee111122223333444455556666", "running", None, None),
        ],
    )

    active = _active_super_agent_thread_ids(
        state_path=tmp_path / "missing-state.json",
        db_path=db_path,
    )

    assert active == {
        "0197aaaa-1111-2222-3333-444455556666",
        "0197bbbb-1111-2222-3333-444455556666",
        # Prefixed codex store ids are mapped back to thread UUIDs.
        "0197dddd-1111-2222-3333-444455556666",
    }


def test_active_super_agent_thread_ids_merges_sqlite_and_legacy_state(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "store" / "state.sqlite3"
    _create_super_agents_store(
        db_path,
        [("s_running", "running", None, "0197aaaa-1111-2222-3333-444455556666")],
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "sessions": {
                    "legacy-thread": {
                        "threadId": "legacy-thread",
                        "lastStatus": "running",
                        "activeTurnId": "turn-1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    active = _active_super_agent_thread_ids(state_path=state_path, db_path=db_path)

    assert active == {
        "0197aaaa-1111-2222-3333-444455556666",
        "legacy-thread",
    }


def test_active_super_agent_thread_ids_honors_store_home_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store_home = tmp_path / "store"
    _create_super_agents_store(
        store_home / "state.sqlite3",
        [("s_running", "running", None, "0197aaaa-1111-2222-3333-444455556666")],
    )
    monkeypatch.setenv("SUPER_AGENTS_CLAUDE_CODE_HOME", str(store_home))

    active = _active_super_agent_thread_ids(state_path=tmp_path / "missing-state.json")

    assert active == {"0197aaaa-1111-2222-3333-444455556666"}


def test_state_db_path_prefers_newest_schema_version(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _create_state_db(home / "state_5.sqlite")
    _create_state_db(home / "state_6.sqlite")
    (home / "state.db").write_text("", encoding="utf-8")

    assert state_db_path(home) == home / "state_6.sqlite"


def test_state_db_path_defaults_when_no_versioned_db(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    assert state_db_path(home) == home / "state_5.sqlite"




def _thread_row_for(rollout_path: Path) -> dict:
    return {"rollout_path": str(rollout_path)}


def test_thread_safe_for_sync_tolerates_truncated_tail(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _create_state_db(home / "state_5.sqlite")
    rollout = _insert_thread(home, "thread-1", title="Thread title", updated_at=20)
    _append_terminal(rollout)
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write('{"timestamp": "2026-05-21T12:00:01.000Z", "type": "eve')

    safety = _thread_safe_for_sync(
        _thread_row_for(rollout), home, "thread-1", stability_delay_seconds=0
    )

    assert safety == ThreadSyncSafety(True, "safe")


def test_thread_safe_for_sync_treats_open_truncated_rollout_as_active(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _create_state_db(home / "state_5.sqlite")
    rollout = _insert_thread(home, "thread-1", title="Thread title", updated_at=20)
    _append_terminal(rollout)
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write('{"timestamp": "2026-05-21T12:00:01.000Z", "type": "eve')
        handle.flush()

        safety = _thread_safe_for_sync(
            _thread_row_for(rollout), home, "thread-1", stability_delay_seconds=0
        )

    assert safety == ThreadSyncSafety(False, "skipped_active")


def test_thread_safe_for_sync_marks_rollout_without_events_malformed(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _create_state_db(home / "state_5.sqlite")
    rollout = _insert_thread(home, "thread-1", title="Thread title", updated_at=20)
    rollout.write_text("not json at all\n", encoding="utf-8")

    safety = _thread_safe_for_sync(
        _thread_row_for(rollout), home, "thread-1", stability_delay_seconds=0
    )

    assert safety == ThreadSyncSafety(False, "rollout_malformed")
