from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from openbase_coder_cli.thread_sync import thread_exchange
from openbase_coder_cli.thread_sync.thread_exchange import (
    export_thread_snapshots,
    get_or_create_device_identity,
    import_thread_snapshots,
    resolve_thread_snapshot_conflict,
    thread_snapshot_conflicts_payload,
    thread_snapshot_status,
    thread_sync_conflicts_payload,
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


def _insert_thread(
    home: Path,
    thread_id: str,
    *,
    title: str,
    updated_at: int,
    terminal_message: str = "done",
    cwd: str = "/tmp/project",
) -> Path:
    rollout_path = (
        home
        / "sessions"
        / "2026"
        / "06"
        / "16"
        / f"rollout-2026-06-16T10-00-00-{thread_id}.jsonl"
    )
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    rollout_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": thread_id}}),
                json.dumps(
                    {
                        "timestamp": "2026-06-16T12:00:00.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-1",
                            "last_agent_message": terminal_message,
                        },
                    }
                ),
            ]
        )
        + "\n",
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
                cwd,
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


def _append_index(home: Path, thread_id: str, name: str) -> None:
    with (home / "session_index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "id": thread_id,
                    "thread_name": name,
                    "updated_at": "2026-06-16T12:00:00Z",
                }
            )
            + "\n"
        )


def _append_dynamic_tool(home: Path, thread_id: str, name: str) -> None:
    with sqlite3.connect(home / "state_5.sqlite") as conn:
        conn.execute(
            """
            INSERT INTO thread_dynamic_tools (
                thread_id, position, name, description, input_schema
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (thread_id, 0, name, "Example", "{}"),
        )


def test_device_identity_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "device.json"

    first = get_or_create_device_identity(path)
    second = get_or_create_device_identity(path)

    assert first.device_id == second.device_id
    assert first.device_name == second.device_name


def test_export_thread_snapshot_writes_metadata_and_rollout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    exchange_dir = tmp_path / "exchange"
    _create_state_db(home / "state_5.sqlite")
    source_rollout = _insert_thread(
        home, "thread-1", title="Thread title", updated_at=20
    )
    _append_index(home, "thread-1", "Indexed title")
    _append_dynamic_tool(home, "thread-1", "example_tool")

    results = export_thread_snapshots(
        codex_home=home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "device.json",
        ledger_path=tmp_path / "ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )

    exported = [result for result in results if result.status == "exported"]
    assert len(exported) == 1
    snapshot_dir = Path(exported[0].snapshot_path or "")
    metadata = json.loads((snapshot_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["thread_id"] == "thread-1"
    assert metadata["thread_row"]["title"] == "Thread title"
    assert "rollout_path" not in metadata["thread_row"]
    assert metadata["session_index_entry"]["thread_name"] == "Indexed title"
    assert metadata["dynamic_tools"][0]["name"] == "example_tool"
    assert (snapshot_dir / "rollout.jsonl").read_text(
        encoding="utf-8"
    ) == source_rollout.read_text(encoding="utf-8")


def test_import_snapshot_creates_local_thread_state(tmp_path: Path) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    _create_state_db(source_home / "state_5.sqlite")
    _create_state_db(target_home / "state_5.sqlite")
    _insert_thread(source_home, "thread-1", title="Thread title", updated_at=20)
    _append_index(source_home, "thread-1", "Indexed title")
    _append_dynamic_tool(source_home, "thread-1", "example_tool")
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )

    results = import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=tmp_path / "target-ledger.json",
    )

    assert [result.status for result in results] == ["imported"]
    with sqlite3.connect(target_home / "state_5.sqlite") as conn:
        row = conn.execute(
            "SELECT title, rollout_path FROM threads WHERE id = ?",
            ("thread-1",),
        ).fetchone()
        tool_row = conn.execute(
            "SELECT name FROM thread_dynamic_tools WHERE thread_id = ?",
            ("thread-1",),
        ).fetchone()
    assert row[0] == "Thread title"
    assert Path(row[1]).exists()
    assert tool_row == ("example_tool",)
    index_lines = (
        (target_home / "session_index.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert json.loads(index_lines[-1])["thread_name"] == "Indexed title"


def test_import_snapshot_translates_source_home_cwd(tmp_path: Path) -> None:
    source_home = tmp_path / "source-codex"
    target_home = tmp_path / "target-codex"
    exchange_dir = tmp_path / "exchange"
    source_user_home = Path("/Users/gabe")
    target_user_home = Path("/home/ubuntu")
    _create_state_db(source_home / "state_5.sqlite")
    _create_state_db(target_home / "state_5.sqlite")
    _insert_thread(
        source_home,
        "thread-1",
        title="Thread title",
        updated_at=20,
        cwd="/Users/gabe/Projects/openbase/code/openbase-coder-workspace",
    )
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
        source_user_home=source_user_home,
    )

    import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=tmp_path / "target-ledger.json",
        target_user_home=target_user_home,
    )

    with sqlite3.connect(target_home / "state_5.sqlite") as conn:
        cwd = conn.execute(
            "SELECT cwd FROM threads WHERE id = ?", ("thread-1",)
        ).fetchone()[0]
    assert cwd == "/home/ubuntu/Projects/openbase/code/openbase-coder-workspace"


def test_import_migrates_existing_foreign_home_cwd(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    _create_state_db(codex_home / "state_5.sqlite")
    _insert_thread(
        codex_home,
        "thread-1",
        title="Thread title",
        updated_at=20,
        cwd="/Users/gabe/Projects/example",
    )

    import_thread_snapshots(
        codex_home=codex_home,
        exchange_dir=tmp_path / "empty-exchange",
        device_identity_path=tmp_path / "device.json",
        ledger_path=tmp_path / "ledger.json",
        target_user_home=Path("/home/ubuntu"),
    )

    with sqlite3.connect(codex_home / "state_5.sqlite") as conn:
        cwd = conn.execute(
            "SELECT cwd FROM threads WHERE id = ?", ("thread-1",)
        ).fetchone()[0]
    assert cwd == "/home/ubuntu/Projects/example"


def test_import_snapshot_is_idempotent(tmp_path: Path) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    target_ledger = tmp_path / "target-ledger.json"
    _create_state_db(source_home / "state_5.sqlite")
    _create_state_db(target_home / "state_5.sqlite")
    _insert_thread(source_home, "thread-1", title="Thread title", updated_at=20)
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )

    first = import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )
    second = import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )

    assert first[0].status == "imported"
    assert second[0].status == "already_imported"


def test_import_snapshot_db_failure_rolls_back_new_rollout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    _create_state_db(source_home / "state_5.sqlite")
    _create_state_db(target_home / "state_5.sqlite")
    source_rollout = _insert_thread(
        source_home, "thread-1", title="Thread title", updated_at=20
    )
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )

    def fail_db_state(**_kwargs):
        raise RuntimeError("db write failed")

    monkeypatch.setattr(thread_exchange, "_apply_snapshot_db_state", fail_db_state)

    with pytest.raises(RuntimeError, match="db write failed"):
        import_thread_snapshots(
            codex_home=target_home,
            exchange_dir=exchange_dir,
            device_identity_path=tmp_path / "target-device.json",
            ledger_path=tmp_path / "target-ledger.json",
        )

    target_rollout = target_home / source_rollout.relative_to(source_home)
    assert not target_rollout.exists()
    assert not (target_home / ".codex-thread-sync-staging").exists()
    assert not (target_home / ".codex-thread-sync-backups").exists()
    with sqlite3.connect(target_home / "state_5.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM threads").fetchone() == (0,)


def test_import_snapshot_db_failure_restores_backed_up_rollout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    target_ledger = tmp_path / "target-ledger.json"
    _create_state_db(source_home / "state_5.sqlite")
    _create_state_db(target_home / "state_5.sqlite")
    source_rollout = _insert_thread(
        source_home, "thread-1", title="Thread title", updated_at=20
    )
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    first = import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )
    assert first[0].status == "imported"
    target_rollout = target_home / source_rollout.relative_to(source_home)
    original_content = target_rollout.read_text(encoding="utf-8")

    # Advance the source thread so a second snapshot supersedes the first.
    with source_rollout.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-06-16T13:00:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-2",
                        "last_agent_message": "more work",
                    },
                }
            )
            + "\n"
        )
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )

    def fail_db_state(**_kwargs):
        raise RuntimeError("db write failed")

    monkeypatch.setattr(thread_exchange, "_apply_snapshot_db_state", fail_db_state)

    with pytest.raises(RuntimeError, match="db write failed"):
        import_thread_snapshots(
            codex_home=target_home,
            exchange_dir=exchange_dir,
            device_identity_path=tmp_path / "target-device.json",
            ledger_path=target_ledger,
        )

    assert target_rollout.read_text(encoding="utf-8") == original_content
    assert not (target_home / ".codex-thread-sync-staging").exists()
    assert not (target_home / ".codex-thread-sync-backups").exists()
    with sqlite3.connect(target_home / "state_5.sqlite") as conn:
        row = conn.execute(
            "SELECT title FROM threads WHERE id = ?",
            ("thread-1",),
        ).fetchone()
    assert row == ("Thread title",)


def test_export_skips_thread_active_in_super_agents_sqlite_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    store_home = tmp_path / "store"
    store_home.mkdir()
    _create_state_db(home / "state_5.sqlite")
    _insert_thread(home, "thread-1", title="Thread title", updated_at=20)
    with sqlite3.connect(store_home / "state.sqlite3") as conn:
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
        conn.execute(
            "insert into sessions values (?, ?, ?, ?)",
            ("codex_session", "running", "turn-1", "thread-1"),
        )
    monkeypatch.setenv("SUPER_AGENTS_CLAUDE_CODE_HOME", str(store_home))

    results = export_thread_snapshots(
        codex_home=home,
        exchange_dir=tmp_path / "exchange",
        device_identity_path=tmp_path / "device.json",
        ledger_path=tmp_path / "ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )

    assert [result.status for result in results] == ["skipped"]
    assert results[0].reason == "skipped_active"


def test_import_snapshot_detects_divergent_local_thread(tmp_path: Path) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    target_ledger = tmp_path / "target-ledger.json"
    _create_state_db(source_home / "state_5.sqlite")
    _create_state_db(target_home / "state_5.sqlite")
    _insert_thread(
        source_home,
        "thread-1",
        title="Remote title",
        updated_at=20,
        terminal_message="remote done",
    )
    _insert_thread(
        target_home,
        "thread-1",
        title="Local title",
        updated_at=30,
        terminal_message="local done",
    )
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )

    results = import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )

    assert results[0].status == "conflict"
    assert results[0].reason == "divergent_fingerprint"
    status = thread_snapshot_status(
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )
    assert status["conflict_count"] == 1


def test_thread_sync_conflicts_payload_includes_home_and_device_conflicts(
    tmp_path: Path,
) -> None:
    normal_home = tmp_path / "normal"
    voice_home = tmp_path / "voice"
    source_home = tmp_path / "source"
    exchange_dir = tmp_path / "exchange"
    home_ledger = tmp_path / "home-ledger.json"
    device_ledger = tmp_path / "device-ledger.json"
    source_device = tmp_path / "source-device.json"
    target_device = tmp_path / "target-device.json"
    for home in (normal_home, voice_home, source_home):
        _create_state_db(home / "state_5.sqlite")

    _insert_thread(
        normal_home,
        "thread-home",
        title="Normal title",
        updated_at=20,
        terminal_message="normal",
    )
    _insert_thread(
        voice_home,
        "thread-home",
        title="Voice title",
        updated_at=30,
        terminal_message="voice",
    )
    home_ledger.write_text(
        json.dumps(
            {
                "threads": {
                    "thread-home": {
                        "thread_id": "thread-home",
                        "normal": {"rollout_sha256": "normal"},
                        "voice": {"rollout_sha256": "voice"},
                        "status": "conflict",
                        "reason": "both_homes_changed",
                        "synced_at": 123.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    _insert_thread(
        source_home,
        "thread-device",
        title="Remote title",
        updated_at=40,
        terminal_message="remote",
    )
    _insert_thread(
        voice_home,
        "thread-device",
        title="Local title",
        updated_at=50,
        terminal_message="local",
    )
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=source_device,
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    import_thread_snapshots(
        codex_home=voice_home,
        exchange_dir=exchange_dir,
        device_identity_path=target_device,
        ledger_path=device_ledger,
    )

    payload = thread_sync_conflicts_payload(
        normal_home=normal_home,
        voice_home=voice_home,
        home_ledger_path=home_ledger,
        exchange_dir=exchange_dir,
        device_identity_path=target_device,
        device_ledger_path=device_ledger,
    )

    assert payload["conflict_count"] == 2
    assert payload["home_conflict_count"] == 1
    assert payload["device_conflict_count"] == 1
    assert {conflict["source_type"] for conflict in payload["conflicts"]} == {
        "home",
        "device",
    }


def test_resolve_conflict_accept_remote_latest_overwrites_local_thread(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    target_ledger = tmp_path / "target-ledger.json"
    source_device = tmp_path / "source-device.json"
    target_device = tmp_path / "target-device.json"
    _create_state_db(source_home / "state_5.sqlite")
    _create_state_db(target_home / "state_5.sqlite")
    _insert_thread(
        source_home,
        "thread-1",
        title="Remote title",
        updated_at=20,
        terminal_message="remote done",
    )
    _insert_thread(
        target_home,
        "thread-1",
        title="Local title",
        updated_at=30,
        terminal_message="local done",
    )
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=source_device,
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=target_device,
        ledger_path=target_ledger,
    )

    result = resolve_thread_snapshot_conflict(
        "thread-1",
        action="accept_remote_latest",
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=target_device,
        ledger_path=target_ledger,
    )

    assert result["conflicts"]["conflict_count"] == 0
    with sqlite3.connect(target_home / "state_5.sqlite") as conn:
        row = conn.execute(
            "SELECT title, preview FROM threads WHERE id = ?",
            ("thread-1",),
        ).fetchone()
    assert row == ("Remote title", "Remote title")
    status = thread_snapshot_conflicts_payload(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=target_device,
        ledger_path=target_ledger,
    )
    assert status["conflict_count"] == 0


def test_resolve_conflict_accept_local_ignores_remote_snapshot(tmp_path: Path) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    target_ledger = tmp_path / "target-ledger.json"
    source_device = tmp_path / "source-device.json"
    target_device = tmp_path / "target-device.json"
    _create_state_db(source_home / "state_5.sqlite")
    _create_state_db(target_home / "state_5.sqlite")
    _insert_thread(
        source_home,
        "thread-1",
        title="Remote title",
        updated_at=20,
        terminal_message="remote done",
    )
    _insert_thread(
        target_home,
        "thread-1",
        title="Local title",
        updated_at=30,
        terminal_message="local done",
    )
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=source_device,
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=target_device,
        ledger_path=target_ledger,
    )

    result = resolve_thread_snapshot_conflict(
        "thread-1",
        action="accept_local",
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=target_device,
        ledger_path=target_ledger,
    )
    second_import = import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=target_device,
        ledger_path=target_ledger,
    )

    assert result["conflicts"]["conflict_count"] == 0
    assert second_import[0].status == "already_imported"
    with sqlite3.connect(target_home / "state_5.sqlite") as conn:
        row = conn.execute(
            "SELECT title, preview FROM threads WHERE id = ?",
            ("thread-1",),
        ).fetchone()
    assert row == ("Local title", "Local title")


def test_import_fast_forwards_snapshot_that_extends_local_rollout(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    target_ledger = tmp_path / "target-ledger.json"
    _create_state_db(source_home / "state_5.sqlite")
    _create_state_db(target_home / "state_5.sqlite")
    source_rollout = _insert_thread(
        source_home, "thread-1", title="Thread title", updated_at=20
    )
    target_rollout = _insert_thread(
        target_home, "thread-1", title="Thread title", updated_at=20
    )
    with source_rollout.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-06-16T12:05:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-2",
                        "last_agent_message": "more work done",
                    },
                }
            )
            + "\n"
        )

    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    results = import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )

    assert [(result.status, result.reason) for result in results] == [
        ("imported", "snapshot_imported")
    ]
    assert target_rollout.read_text(encoding="utf-8") == source_rollout.read_text(
        encoding="utf-8"
    )
    status = thread_snapshot_status(
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )
    assert status["conflict_count"] == 0


def test_import_ignores_stale_snapshot_superseded_by_local_rollout(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    target_ledger = tmp_path / "target-ledger.json"
    _create_state_db(source_home / "state_5.sqlite")
    _create_state_db(target_home / "state_5.sqlite")
    _insert_thread(source_home, "thread-1", title="Thread title", updated_at=20)
    target_rollout = _insert_thread(
        target_home, "thread-1", title="Thread title", updated_at=20
    )
    with target_rollout.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-06-16T12:05:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-2",
                        "last_agent_message": "more work done",
                    },
                }
            )
            + "\n"
        )
    local_before = target_rollout.read_text(encoding="utf-8")

    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    results = import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )

    assert [(result.status, result.reason) for result in results] == [
        ("skipped", "superseded_by_local")
    ]
    assert target_rollout.read_text(encoding="utf-8") == local_before
    status = thread_snapshot_status(
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )
    assert status["conflict_count"] == 0


def test_import_auto_clears_conflict_after_content_converges(tmp_path: Path) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    target_ledger = tmp_path / "target-ledger.json"
    _create_state_db(source_home / "state_5.sqlite")
    _create_state_db(target_home / "state_5.sqlite")
    source_rollout = _insert_thread(
        source_home,
        "thread-1",
        title="Remote title",
        updated_at=20,
        terminal_message="remote done",
    )
    target_rollout = _insert_thread(
        target_home,
        "thread-1",
        title="Local title",
        updated_at=30,
        terminal_message="local done",
    )
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    first = import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )
    assert first[0].status == "conflict"

    # The sides converge (for example the divergent branch was resolved on
    # the source device), then the source exports a snapshot matching the
    # local rollout byte-for-byte.
    source_rollout.write_text(
        target_rollout.read_text(encoding="utf-8"), encoding="utf-8"
    )
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    second = import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )

    outcomes = {(result.status, result.reason) for result in second}
    assert ("already_imported", "same_content") in outcomes
    status = thread_snapshot_status(
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )
    assert status["conflict_count"] == 0

    # The stale divergent snapshot must not re-mint the cleared conflict.
    third = import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )
    assert all(result.status != "conflict" for result in third)
    status = thread_snapshot_status(
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )
    assert status["conflict_count"] == 0


def _exported_snapshot_metadata_path(exchange_dir: Path) -> Path:
    snapshot_dirs = sorted((exchange_dir / "devices").glob("*/snapshots/*/*"))
    assert len(snapshot_dirs) == 1
    return snapshot_dirs[0] / "metadata.json"


def test_import_caches_invalid_snapshot_and_revalidates_on_change(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    ledger_path = tmp_path / "target-ledger.json"
    _create_state_db(source_home / "state_5.sqlite")
    _create_state_db(target_home / "state_5.sqlite")
    _insert_thread(source_home, "thread-1", title="Thread title", updated_at=20)
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    metadata_path = _exported_snapshot_metadata_path(exchange_dir)
    valid_metadata = metadata_path.read_text(encoding="utf-8")
    metadata_path.write_text("not json", encoding="utf-8")

    def run_import() -> list:
        return import_thread_snapshots(
            codex_home=target_home,
            exchange_dir=exchange_dir,
            device_identity_path=tmp_path / "target-device.json",
            ledger_path=ledger_path,
        )

    first = run_import()
    assert [result.status for result in first] == ["skipped"]
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(ledger["invalid_snapshots"]) == 1

    # An unchanged invalid snapshot must not be re-read: make the metadata
    # unreadable so any parse attempt would raise.
    metadata_path.chmod(0o000)
    try:
        second = run_import()
    finally:
        metadata_path.chmod(0o600)
    assert [(result.status, result.reason) for result in second] == [
        (first[0].status, first[0].reason)
    ]

    metadata_path.write_text(valid_metadata, encoding="utf-8")
    third = run_import()
    assert [result.status for result in third] == ["imported"]
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["invalid_snapshots"] == {}


def test_import_prunes_cache_entries_for_removed_snapshots(tmp_path: Path) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    ledger_path = tmp_path / "target-ledger.json"
    _create_state_db(source_home / "state_5.sqlite")
    _create_state_db(target_home / "state_5.sqlite")
    _insert_thread(source_home, "thread-1", title="Thread title", updated_at=20)
    export_thread_snapshots(
        codex_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    metadata_path = _exported_snapshot_metadata_path(exchange_dir)
    metadata_path.write_text("not json", encoding="utf-8")

    import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=ledger_path,
    )
    assert (
        len(json.loads(ledger_path.read_text(encoding="utf-8"))["invalid_snapshots"])
        == 1
    )

    shutil.rmtree(metadata_path.parent)
    import_thread_snapshots(
        codex_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=ledger_path,
    )
    assert (
        json.loads(ledger_path.read_text(encoding="utf-8"))["invalid_snapshots"] == {}
    )
