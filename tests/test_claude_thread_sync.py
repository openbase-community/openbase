from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from openbase_coder_cli.thread_sync import claude_snapshot_io
from openbase_coder_cli.thread_sync.claude_thread_sync import (
    ClaudeConflictResolutionError,
    claude_thread_snapshot_status,
    export_claude_thread_snapshots,
    import_claude_thread_snapshots,
    resolve_claude_snapshot_conflict,
)


def _project_key(cwd: str) -> str:
    return cwd.replace("/", "-")


def _session_path(home: Path, cwd: str, session_id: str) -> Path:
    return home / "projects" / _project_key(cwd) / f"{session_id}.jsonl"


def _write_session(
    home: Path,
    cwd: str,
    session_id: str,
    *,
    user_text: str = "Build the thing",
    assistant_text: str = "Done.",
    extra_events: list[dict] | None = None,
) -> Path:
    path = _session_path(home, cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "user",
            "sessionId": session_id,
            "cwd": cwd,
            "timestamp": "2026-06-20T12:00:00.000Z",
            "message": {"role": "user", "content": user_text},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "cwd": cwd,
            "timestamp": "2026-06-20T12:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            },
        },
        *(extra_events or []),
    ]
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


def _seed_device_conflict(tmp_path: Path, session_id: str) -> dict[str, Path]:
    """Create a divergent local/remote Claude session and record the conflict."""
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    target_ledger = tmp_path / "target-ledger.json"
    _write_session(source_home, "/tmp/project", session_id, assistant_text="Remote")
    _write_session(target_home, "/tmp/project", session_id, assistant_text="Local")
    export_claude_thread_snapshots(
        claude_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        super_agents_db_path=tmp_path / "source-state.sqlite3",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    results = import_claude_thread_snapshots(
        claude_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
        super_agents_db_path=tmp_path / "target-state.sqlite3",
    )
    assert results[0].status == "conflict"
    return {
        "source_home": source_home,
        "target_home": target_home,
        "exchange_dir": exchange_dir,
        "target_ledger": target_ledger,
        "target_device": tmp_path / "target-device.json",
        "target_state_db": tmp_path / "target-state.sqlite3",
    }


def test_export_claude_thread_snapshot_writes_metadata_and_companions(
    tmp_path: Path,
) -> None:
    openbase_home = tmp_path / "openbase"
    exchange_dir = tmp_path / "exchange"
    session_id = "1153fd55-3866-4408-bf95-499aa32d3c0f"
    source = _write_session(openbase_home, "/tmp/project", session_id)
    tool_result = source.parent / session_id / "tool-results" / "result.txt"
    tool_result.parent.mkdir(parents=True)
    tool_result.write_text("tool output", encoding="utf-8")

    results = export_claude_thread_snapshots(
        claude_home=openbase_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        super_agents_db_path=tmp_path / "state.sqlite3",
        stability_delay_seconds=0,
        max_age_days=None,
    )

    assert results[0].status == "exported"
    snapshot_dir = Path(results[0].snapshot_path or "")
    metadata = json.loads((snapshot_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["session_id"] == session_id
    assert (
        metadata["root_relative_path"] == source.relative_to(openbase_home).as_posix()
    )
    assert source.relative_to(openbase_home).as_posix() in metadata["files"]
    assert (snapshot_dir / "files" / tool_result.relative_to(openbase_home)).read_text(
        encoding="utf-8"
    ) == "tool output"


def test_import_claude_thread_snapshot_creates_session_and_backfills_metadata(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    db_path = tmp_path / "target-state.sqlite3"
    session_id = "1416448e-c428-455b-bceb-5ac34da8ee4e"
    source_cwd = "/Users/example/Projects/openbase/code/openbase-coder-workspace"
    target_cwd = "/home/ubuntu/Projects/openbase/code/openbase-coder-workspace"
    _write_session(
        source_home,
        source_cwd,
        session_id,
        user_text="Build cross device",
        assistant_text="Cross device done.",
    )
    export_claude_thread_snapshots(
        claude_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        super_agents_db_path=tmp_path / "source-state.sqlite3",
        stability_delay_seconds=0,
        max_age_days=None,
        source_user_home=Path("/Users/example"),
    )

    results = import_claude_thread_snapshots(
        claude_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=tmp_path / "target-ledger.json",
        super_agents_db_path=db_path,
        target_user_home=Path("/home/ubuntu"),
    )

    assert results[0].status == "imported"
    target_session = _session_path(target_home, source_cwd, session_id)
    assert target_session.exists()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select name, backend_session_id, last_useful_message, cwd from sessions"
        ).fetchone()
    assert row == (
        "Build cross device",
        session_id,
        "Cross device done.",
        target_cwd,
    )


def test_import_claude_snapshots_migrates_existing_foreign_home_cwd(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table sessions (
                id text primary key,
                cwd text not null
            )
            """
        )
        conn.execute(
            "insert into sessions (id, cwd) values (?, ?)",
            ("session-1", "/Users/example/Projects/example"),
        )

    import_claude_thread_snapshots(
        claude_home=tmp_path / "openbase-home",
        exchange_dir=tmp_path / "empty-exchange",
        device_identity_path=tmp_path / "device.json",
        ledger_path=tmp_path / "ledger.json",
        super_agents_db_path=db_path,
        target_user_home=Path("/home/ubuntu"),
    )

    with sqlite3.connect(db_path) as conn:
        cwd = conn.execute(
            "select cwd from sessions where id = ?", ("session-1",)
        ).fetchone()[0]
    assert cwd == "/home/ubuntu/Projects/example"


def test_import_claude_thread_snapshot_is_idempotent(tmp_path: Path) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    session_id = "1aaa22b0-d887-4d15-8ed6-beafb084a924"
    _write_session(source_home, "/tmp/project", session_id)
    export_claude_thread_snapshots(
        claude_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        super_agents_db_path=tmp_path / "source-state.sqlite3",
        stability_delay_seconds=0,
        max_age_days=None,
    )

    first = import_claude_thread_snapshots(
        claude_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=tmp_path / "target-ledger.json",
        super_agents_db_path=tmp_path / "target-state.sqlite3",
    )
    second = import_claude_thread_snapshots(
        claude_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=tmp_path / "target-ledger.json",
        super_agents_db_path=tmp_path / "target-state.sqlite3",
    )

    assert first[0].status == "imported"
    assert second[0].status == "already_imported"


def test_import_claude_thread_snapshot_failed_commit_leaves_no_visible_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    session_id = "4a91cc85-7601-42f1-a8c1-dcb8c58418ea"
    _write_session(source_home, "/tmp/project", session_id)
    export_claude_thread_snapshots(
        claude_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        super_agents_db_path=tmp_path / "source-state.sqlite3",
        stability_delay_seconds=0,
        max_age_days=None,
    )

    def fail_commit(**_kwargs):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(claude_snapshot_io, "_commit_staged_session", fail_commit)

    results = import_claude_thread_snapshots(
        claude_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=tmp_path / "target-ledger.json",
        super_agents_db_path=tmp_path / "target-state.sqlite3",
    )

    assert results[0].status == "error"
    assert results[0].reason == "import_failed"
    assert not _session_path(target_home, "/tmp/project", session_id).exists()
    assert not (target_home / ".claude-thread-sync-staging").exists()


def test_import_claude_thread_snapshot_records_conflict_evidence(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    target_ledger = tmp_path / "target-ledger.json"
    session_id = "1aca9dce-dd37-4558-a56a-8677dd981e34"
    _write_session(source_home, "/tmp/project", session_id, assistant_text="Remote")
    _write_session(target_home, "/tmp/project", session_id, assistant_text="Local")
    export_claude_thread_snapshots(
        claude_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        super_agents_db_path=tmp_path / "source-state.sqlite3",
        stability_delay_seconds=0,
        max_age_days=None,
    )

    results = import_claude_thread_snapshots(
        claude_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
        super_agents_db_path=tmp_path / "target-state.sqlite3",
    )

    assert results[0].status == "conflict"
    assert results[0].reason == "divergent_fingerprint"
    status = claude_thread_snapshot_status(
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )
    assert status["conflict_count"] == 1
    assert status["conflicts"][0]["session_id"] == session_id
    assert status["conflicts"][0]["snapshot_path"]


def test_resolve_claude_conflict_accept_remote_latest_imports_snapshot(
    tmp_path: Path,
) -> None:
    session_id = "b7d51d2e-51fd-4d29-a865-6bd0e0be0339"
    paths = _seed_device_conflict(tmp_path, session_id)

    result = resolve_claude_snapshot_conflict(
        session_id,
        action="accept_remote_latest",
        claude_home=paths["target_home"],
        exchange_dir=paths["exchange_dir"],
        device_identity_path=paths["target_device"],
        ledger_path=paths["target_ledger"],
        super_agents_db_path=paths["target_state_db"],
    )

    assert result["action"] == "accept_remote_latest"
    assert result["conflicts"]["conflict_count"] == 0
    target_session = _session_path(paths["target_home"], "/tmp/project", session_id)
    assert '"Remote"' in target_session.read_text(encoding="utf-8")
    with sqlite3.connect(paths["target_state_db"]) as conn:
        row = conn.execute(
            "select backend_session_id, last_useful_message from sessions"
        ).fetchone()
    assert row == (session_id, "Remote")
    status = claude_thread_snapshot_status(
        exchange_dir=paths["exchange_dir"],
        device_identity_path=paths["target_device"],
        ledger_path=paths["target_ledger"],
    )
    assert status["conflict_count"] == 0
    second_import = import_claude_thread_snapshots(
        claude_home=paths["target_home"],
        exchange_dir=paths["exchange_dir"],
        device_identity_path=paths["target_device"],
        ledger_path=paths["target_ledger"],
        super_agents_db_path=paths["target_state_db"],
    )
    assert second_import[0].status == "already_imported"


def test_resolve_claude_conflict_accept_local_ignores_remote_snapshot(
    tmp_path: Path,
) -> None:
    session_id = "51f7c9e2-4c25-4aca-9f0f-2c22c72ad2ef"
    paths = _seed_device_conflict(tmp_path, session_id)

    result = resolve_claude_snapshot_conflict(
        session_id,
        action="accept_local",
        claude_home=paths["target_home"],
        exchange_dir=paths["exchange_dir"],
        device_identity_path=paths["target_device"],
        ledger_path=paths["target_ledger"],
        super_agents_db_path=paths["target_state_db"],
    )
    second_import = import_claude_thread_snapshots(
        claude_home=paths["target_home"],
        exchange_dir=paths["exchange_dir"],
        device_identity_path=paths["target_device"],
        ledger_path=paths["target_ledger"],
        super_agents_db_path=paths["target_state_db"],
    )

    assert result["action"] == "accept_local"
    assert result["conflicts"]["conflict_count"] == 0
    assert second_import[0].status == "already_imported"
    target_session = _session_path(paths["target_home"], "/tmp/project", session_id)
    assert '"Local"' in target_session.read_text(encoding="utf-8")


def test_resolve_claude_conflict_refuses_active_session(tmp_path: Path) -> None:
    session_id = "0d5720c6-6c2c-45f0-8f27-8bd4ba03be7b"
    paths = _seed_device_conflict(tmp_path, session_id)
    with sqlite3.connect(paths["target_state_db"]) as conn:
        conn.execute(
            """
            create table if not exists sessions (
                id text primary key,
                name text not null unique,
                cwd text not null,
                command_json text not null,
                status text not null,
                active_turn_id text,
                backend_session_id text,
                created_at text not null,
                updated_at text not null
            )
            """
        )
        conn.execute(
            """
            insert into sessions (
                id, name, cwd, command_json, status, active_turn_id,
                backend_session_id, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "s_active",
                "active",
                "/tmp/project",
                "[]",
                "running",
                "t1",
                session_id,
                "2026-06-20T12:00:00.000Z",
                "2026-06-20T12:00:00.000Z",
            ),
        )

    with pytest.raises(ClaudeConflictResolutionError, match="session_active"):
        resolve_claude_snapshot_conflict(
            session_id,
            action="accept_remote_latest",
            claude_home=paths["target_home"],
            exchange_dir=paths["exchange_dir"],
            device_identity_path=paths["target_device"],
            ledger_path=paths["target_ledger"],
            super_agents_db_path=paths["target_state_db"],
        )

    target_session = _session_path(paths["target_home"], "/tmp/project", session_id)
    assert '"Local"' in target_session.read_text(encoding="utf-8")


def test_resolve_claude_conflict_requires_existing_conflict(tmp_path: Path) -> None:
    with pytest.raises(ClaudeConflictResolutionError, match="conflict_not_found"):
        resolve_claude_snapshot_conflict(
            "b3a5b6f1-64f7-40dd-b7d5-3e19e2b1f0aa",
            action="accept_local",
            claude_home=tmp_path / "target",
            exchange_dir=tmp_path / "exchange",
            device_identity_path=tmp_path / "target-device.json",
            ledger_path=tmp_path / "target-ledger.json",
            super_agents_db_path=tmp_path / "target-state.sqlite3",
        )


def test_import_treats_companion_churn_with_matching_transcript_as_same_content(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    target_ledger = tmp_path / "target-ledger.json"
    session_id = "51f8f6f3-1651-49c8-b9f8-1f2937efab61"
    source_root = _write_session(source_home, "/tmp/project", session_id)
    _write_session(target_home, "/tmp/project", session_id)
    companion_dir = source_root.parent / session_id
    companion_dir.mkdir(parents=True, exist_ok=True)
    (companion_dir / "tool-state.json").write_text("{}\n", encoding="utf-8")

    export_claude_thread_snapshots(
        claude_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        super_agents_db_path=tmp_path / "source-state.sqlite3",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    results = import_claude_thread_snapshots(
        claude_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
        super_agents_db_path=tmp_path / "target-state.sqlite3",
    )

    assert [(result.status, result.reason) for result in results] == [
        ("already_imported", "same_content_bytes")
    ]
    status = claude_thread_snapshot_status(
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )
    assert status["conflict_count"] == 0


def test_import_fast_forwards_snapshot_that_extends_local_transcript(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    target_ledger = tmp_path / "target-ledger.json"
    session_id = "9f1f2f9f-8802-4c33-9d4e-53b1f39be186"
    source_root = _write_session(
        source_home,
        "/tmp/project",
        session_id,
        extra_events=[
            {
                "type": "user",
                "sessionId": session_id,
                "cwd": "/tmp/project",
                "timestamp": "2026-06-20T12:00:02.000Z",
                "message": {"role": "user", "content": "Keep going"},
            }
        ],
    )
    target_root = _write_session(target_home, "/tmp/project", session_id)
    assert source_root.read_text(encoding="utf-8").startswith(
        target_root.read_text(encoding="utf-8")
    )

    export_claude_thread_snapshots(
        claude_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        super_agents_db_path=tmp_path / "source-state.sqlite3",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    results = import_claude_thread_snapshots(
        claude_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
        super_agents_db_path=tmp_path / "target-state.sqlite3",
    )

    assert [(result.status, result.reason) for result in results] == [
        ("imported", "snapshot_imported")
    ]
    assert target_root.read_text(encoding="utf-8") == source_root.read_text(
        encoding="utf-8"
    )
    status = claude_thread_snapshot_status(
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )
    assert status["conflict_count"] == 0


def test_import_auto_clears_claude_conflict_after_content_converges(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    exchange_dir = tmp_path / "exchange"
    target_ledger = tmp_path / "target-ledger.json"
    session_id = "77a3af6d-5f27-4f38-9f5f-3a3f8f0b8a1c"
    source_root = _write_session(
        source_home, "/tmp/project", session_id, assistant_text="Remote"
    )
    target_root = _write_session(
        target_home, "/tmp/project", session_id, assistant_text="Local"
    )
    export_claude_thread_snapshots(
        claude_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        super_agents_db_path=tmp_path / "source-state.sqlite3",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    first = import_claude_thread_snapshots(
        claude_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
        super_agents_db_path=tmp_path / "target-state.sqlite3",
    )
    assert first[0].status == "conflict"

    source_root.write_text(target_root.read_text(encoding="utf-8"), encoding="utf-8")
    export_claude_thread_snapshots(
        claude_home=source_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "source-device.json",
        ledger_path=tmp_path / "source-ledger.json",
        super_agents_db_path=tmp_path / "source-state.sqlite3",
        stability_delay_seconds=0,
        max_age_days=None,
    )
    second = import_claude_thread_snapshots(
        claude_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
        super_agents_db_path=tmp_path / "target-state.sqlite3",
    )

    outcomes = {(result.status, result.reason) for result in second}
    assert ("already_imported", "same_content") in outcomes
    status = claude_thread_snapshot_status(
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
    )
    assert status["conflict_count"] == 0

    third = import_claude_thread_snapshots(
        claude_home=target_home,
        exchange_dir=exchange_dir,
        device_identity_path=tmp_path / "target-device.json",
        ledger_path=target_ledger,
        super_agents_db_path=tmp_path / "target-state.sqlite3",
    )
    assert all(result.status != "conflict" for result in third)


def test_meaningful_user_text_strips_harness_markup():
    from openbase_coder_cli.thread_sync.claude_thread_sync import _meaningful_user_text

    assert (
        _meaningful_user_text(
            "<local-command-caveat>Caveat: generated by the user</local-command-caveat>\n"
            "<command-name>/model</command-name>\n"
            "Help me fix the bug"
        )
        == "Help me fix the bug"
    )
    assert _meaningful_user_text("<system-reminder>noise</system-reminder>") is None
    assert _meaningful_user_text("plain question") == "plain question"
