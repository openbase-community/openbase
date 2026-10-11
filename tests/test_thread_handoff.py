"""Single-thread handoff through the exchange folder (real stores, two homes)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from openbase_coder_cli.thread_sync import thread_handoff as handoff
from openbase_coder_cli.thread_sync.thread_handoff import HandoffError, HandoffPaths

CODEX_SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL, source TEXT NOT NULL, model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL, title TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
    cli_version TEXT NOT NULL DEFAULT '', first_user_message TEXT NOT NULL DEFAULT '',
    model TEXT, reasoning_effort TEXT, created_at_ms INTEGER, updated_at_ms INTEGER,
    preview TEXT NOT NULL DEFAULT ''
);
CREATE TABLE thread_dynamic_tools (
    thread_id TEXT NOT NULL, position INTEGER NOT NULL, name TEXT NOT NULL,
    description TEXT NOT NULL, input_schema TEXT NOT NULL,
    defer_loading INTEGER NOT NULL DEFAULT 0, namespace TEXT,
    PRIMARY KEY(thread_id, position)
);
"""


def _codex_home(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path / "state_5.sqlite") as conn:
        conn.executescript(CODEX_SCHEMA)
    return path


def _codex_thread(
    home: Path, thread_id: str, *, cwd: str, terminal: bool = True
) -> Path:
    rollout = home / "sessions" / "2026" / "10" / "08" / f"rollout-x-{thread_id}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    events = [{"type": "session_meta", "payload": {"id": thread_id, "cwd": cwd}}]
    if terminal:
        events.append(
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": "done"},
            }
        )
    rollout.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    with sqlite3.connect(home / "state_5.sqlite") as conn:
        conn.execute(
            "INSERT INTO threads (id, rollout_path, created_at, updated_at, source,"
            " model_provider, cwd, title, sandbox_policy, approval_mode,"
            " created_at_ms, updated_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                thread_id,
                str(rollout),
                1,
                2,
                "cli",
                "openai",
                cwd,
                "Pushed thread",
                "danger-full-access",
                "never",
                1000,
                2000,
            ),
        )
    return rollout


def _claude_session(home: Path, cwd: str, session_id: str) -> Path:
    path = home / "projects" / cwd.replace("/", "-") / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "user",
            "sessionId": session_id,
            "cwd": cwd,
            "timestamp": "2026-10-08T12:00:00.000Z",
            "message": {"role": "user", "content": "Build it"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "cwd": cwd,
            "timestamp": "2026-10-08T12:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Ok"}],
            },
        },
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def _sides(tmp_path: Path) -> tuple[HandoffPaths, HandoffPaths]:
    exchange = tmp_path / "exchange"

    def side(name: str, user_home: str) -> HandoffPaths:
        root = tmp_path / name
        return HandoffPaths(
            exchange_dir=exchange,
            device_identity_path=root / "device.json",
            codex_home=root / "codex",
            codex_ledger_path=root / "codex-ledger.json",
            claude_home=root / "claude",
            claude_ledger_path=root / "claude-ledger.json",
            super_agents_db_path=root / "state.sqlite3",
            user_home=Path(user_home),
        )

    return side("laptop", "/Users/edge"), side("hub", "/Users/hub")


def test_codex_thread_hands_off_with_home_relative_cwd(tmp_path: Path) -> None:
    laptop, hub = _sides(tmp_path)
    _codex_home(laptop.codex_home)
    _codex_home(hub.codex_home)
    _codex_thread(laptop.codex_home, "t-1", cwd="/Users/edge/Projects/app")

    snapshot = handoff.export_one(handoff.CODEX, "t-1", paths=laptop)

    assert handoff.snapshot_present(snapshot, paths=hub)
    assert handoff.import_one(snapshot, paths=hub) == "snapshot_imported"
    with sqlite3.connect(hub.codex_home / "state_5.sqlite") as conn:
        cwd = conn.execute("select cwd from threads where id = 't-1'").fetchone()[0]
    assert cwd == "/Users/hub/Projects/app"
    assert handoff.local_thread_id(snapshot, paths=hub) == "t-1"
    # Retrying the import of the same snapshot is a no-op success.
    assert handoff.import_one(snapshot, paths=hub) == "fingerprint_seen"


def test_codex_export_is_idempotent_and_targets_one_thread(tmp_path: Path) -> None:
    laptop, _hub = _sides(tmp_path)
    _codex_home(laptop.codex_home)
    _codex_thread(laptop.codex_home, "t-1", cwd="/Users/edge/a")
    _codex_thread(laptop.codex_home, "t-2", cwd="/Users/edge/b")

    first = handoff.export_one(handoff.CODEX, "t-1", paths=laptop)
    second = handoff.export_one(handoff.CODEX, "t-1", paths=laptop)

    assert first == second
    snapshots = list((laptop.exchange_dir / "devices").glob("*/snapshots/*"))
    assert [path.name for path in snapshots] == ["t-1"]


def test_export_rewrites_a_pruned_snapshot(tmp_path: Path) -> None:
    laptop, _hub = _sides(tmp_path)
    _codex_home(laptop.codex_home)
    _codex_thread(laptop.codex_home, "t-1", cwd="/Users/edge/a")
    first = handoff.export_one(handoff.CODEX, "t-1", paths=laptop)
    shutil.rmtree(handoff.snapshot_dir(first, paths=laptop))

    again = handoff.export_one(handoff.CODEX, "t-1", paths=laptop)

    assert again.fingerprint == first.fingerprint
    assert handoff.snapshot_present(again, paths=laptop)


def test_unfinished_codex_rollout_is_not_exportable(tmp_path: Path) -> None:
    laptop, _hub = _sides(tmp_path)
    _codex_home(laptop.codex_home)
    _codex_thread(laptop.codex_home, "t-1", cwd="/Users/edge/a", terminal=False)

    with pytest.raises(HandoffError) as caught:
        handoff.export_one(handoff.CODEX, "t-1", paths=laptop)

    assert caught.value.code == "not_exportable"


def test_import_refuses_a_divergent_copy(tmp_path: Path) -> None:
    laptop, hub = _sides(tmp_path)
    _codex_home(laptop.codex_home)
    _codex_home(hub.codex_home)
    _codex_thread(laptop.codex_home, "t-1", cwd="/Users/edge/a")
    rollout = _codex_thread(hub.codex_home, "t-1", cwd="/Users/hub/a")
    rollout.write_text(rollout.read_text() + json.dumps({"type": "x"}) + "\n")
    snapshot = handoff.export_one(handoff.CODEX, "t-1", paths=laptop)

    with pytest.raises(HandoffError) as caught:
        handoff.import_one(snapshot, paths=hub)

    assert caught.value.code == "conflict"


def test_missing_snapshot_is_retryable(tmp_path: Path) -> None:
    laptop, hub = _sides(tmp_path)
    _codex_home(hub.codex_home)
    snapshot = handoff.HandoffSnapshot(handoff.CODEX, "t-9", "fp", "device-x")

    assert not handoff.snapshot_present(snapshot, paths=hub)
    with pytest.raises(HandoffError) as caught:
        handoff.import_one(snapshot, paths=hub)

    assert caught.value.code == "snapshot_missing"
    assert caught.value.retryable


def test_claude_session_hands_off_and_gets_a_local_thread_id(tmp_path: Path) -> None:
    laptop, hub = _sides(tmp_path)
    session_id = "0836b77e-6324-4228-b34c-b4d8e71ee0ea"
    _claude_session(laptop.claude_home, "/Users/edge/Projects/app", session_id)

    snapshot = handoff.export_one(handoff.CLAUDE_CODE, session_id, paths=laptop)
    outcome = handoff.import_one(snapshot, paths=hub)

    assert outcome == "snapshot_imported"
    assert list(hub.claude_home.glob(f"projects/*/{session_id}.jsonl"))
    local_id = handoff.local_thread_id(snapshot, paths=hub)
    assert local_id == "claude_" + session_id.replace("-", "")
    with sqlite3.connect(hub.super_agents_db_path) as conn:
        cwd = conn.execute(
            "select cwd from sessions where backend_session_id = ?", (session_id,)
        ).fetchone()[0]
    assert cwd == "/Users/hub/Projects/app"


def test_claude_thread_without_session_is_rejected() -> None:
    with pytest.raises(HandoffError) as caught:
        handoff.entity_id_for(handoff.CLAUDE_CODE, "claude_x", None)
    assert caught.value.code == "not_exportable"
    assert handoff.entity_id_for(handoff.CODEX, "t-1", None) == "t-1"


def test_unsupported_backend_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(HandoffError) as caught:
        handoff.export_one("gemini", "x", paths=_sides(tmp_path)[0])
    assert caught.value.code == "unsupported_backend"


def test_busy_ledger_lock_is_reported_as_retryable(tmp_path: Path) -> None:
    from openbase_coder_cli.thread_sync.thread_sync_common import ledger_lock

    laptop, _hub = _sides(tmp_path)
    _codex_home(laptop.codex_home)
    _codex_thread(laptop.codex_home, "t-1", cwd="/Users/edge/a")
    original = handoff.LEDGER_LOCK_TIMEOUT_SECONDS
    handoff.LEDGER_LOCK_TIMEOUT_SECONDS = 0.2
    try:
        with ledger_lock(laptop.codex_ledger_path):
            with pytest.raises(HandoffError) as caught:
                handoff.export_one(handoff.CODEX, "t-1", paths=laptop)
    finally:
        handoff.LEDGER_LOCK_TIMEOUT_SECONDS = original

    assert caught.value.code == "sync_busy"
    assert caught.value.retryable


def _append_events(rollout: Path, *payload_types: str) -> None:
    with rollout.open("a", encoding="utf-8") as handle:
        for payload_type in payload_types:
            handle.write(
                json.dumps({"type": "event_msg", "payload": {"type": payload_type}})
                + "\n"
            )


def test_resumed_idle_codex_thread_is_exportable(tmp_path: Path) -> None:
    # Resuming an idle thread (opening it in Openbase) appends its settings
    # after the last finished turn; a late background command completion
    # can follow too. Neither means a turn is running.
    laptop, _hub = _sides(tmp_path)
    _codex_home(laptop.codex_home)
    rollout = _codex_thread(laptop.codex_home, "t-1", cwd="/Users/edge/a")
    _append_events(rollout, "thread_settings_applied", "item_completed")

    snapshot = handoff.export_one(handoff.CODEX, "t-1", paths=laptop)

    assert handoff.snapshot_present(snapshot, paths=laptop)


def test_turn_started_after_settings_is_still_unfinished(tmp_path: Path) -> None:
    laptop, _hub = _sides(tmp_path)
    _codex_home(laptop.codex_home)
    rollout = _codex_thread(laptop.codex_home, "t-1", cwd="/Users/edge/a")
    _append_events(rollout, "thread_settings_applied", "task_started")

    with pytest.raises(HandoffError) as caught:
        handoff.export_one(handoff.CODEX, "t-1", paths=laptop)

    assert caught.value.code == "not_exportable"
