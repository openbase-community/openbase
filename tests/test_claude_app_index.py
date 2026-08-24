from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from openbase_coder_cli import claude_app_index


def _create_store(db_path: Path, rows: list[tuple]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table sessions (
                id text primary key,
                name text,
                cwd text,
                model text,
                backend text,
                backend_session_id text,
                created_at text,
                updated_at text
            )
            """
        )
        conn.executemany(
            "insert into sessions values (?, ?, ?, ?, ?, ?, ?, ?)", rows
        )


def _app_index(tmp_path: Path) -> Path:
    account_dir = tmp_path / "app" / "ws-1" / "acct-1"
    account_dir.mkdir(parents=True)
    (account_dir / "local_existing.json").write_text(
        json.dumps({"sessionId": "local_existing", "cliSessionId": "app-owned"}),
        encoding="utf-8",
    )
    return account_dir


def test_sync_injects_openbase_sessions_into_app_index(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(claude_app_index.platform, "system", lambda: "Darwin")
    account_dir = _app_index(tmp_path)
    db_path = tmp_path / "state.sqlite3"
    _create_store(
        db_path,
        [
            (
                "claude_abc",
                "report-agent",
                "/tmp/project",
                "claude-sonnet-5",
                "claude_code",
                "11111111-2222-3333-4444-555555555555",
                "2026-08-24T10:00:00+00:00",
                "2026-08-24T11:00:00+00:00",
            ),
            # Codex sessions never reach the Claude app index.
            (
                "codex_def",
                "codex-agent",
                "/tmp/project",
                "gpt-5.5",
                "codex",
                "66666666-7777-8888-9999-000000000000",
                "2026-08-24T10:00:00+00:00",
                "2026-08-24T11:00:00+00:00",
            ),
        ],
    )

    result = claude_app_index.sync_claude_app_index(
        app_sessions_dir=tmp_path / "app",
        ledger_path=tmp_path / "ledger.json",
        db_path=db_path,
    )

    assert result["supported"] is True
    assert result["created"] == 1
    entries = sorted(account_dir.glob("local_*.json"))
    payloads = [
        json.loads(entry.read_text(encoding="utf-8")) for entry in entries
    ]
    injected = [
        payload
        for payload in payloads
        if payload.get("cliSessionId") == "11111111-2222-3333-4444-555555555555"
    ]
    assert len(injected) == 1
    assert injected[0]["title"] == "report-agent"
    assert injected[0]["cwd"] == "/tmp/project"
    assert injected[0]["isArchived"] is False
    # The app-owned entry is untouched.
    app_owned = json.loads(
        (account_dir / "local_existing.json").read_text(encoding="utf-8")
    )
    assert app_owned == {"sessionId": "local_existing", "cliSessionId": "app-owned"}


def test_sync_is_idempotent_and_updates_activity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(claude_app_index.platform, "system", lambda: "Darwin")
    account_dir = _app_index(tmp_path)
    db_path = tmp_path / "state.sqlite3"
    _create_store(
        db_path,
        [
            (
                "claude_abc",
                "report-agent",
                "/tmp/project",
                None,
                "claude_code",
                "11111111-2222-3333-4444-555555555555",
                "2026-08-24T10:00:00+00:00",
                "2026-08-24T11:00:00+00:00",
            ),
        ],
    )
    kwargs = dict(
        app_sessions_dir=tmp_path / "app",
        ledger_path=tmp_path / "ledger.json",
        db_path=db_path,
    )

    first = claude_app_index.sync_claude_app_index(**kwargs)
    second = claude_app_index.sync_claude_app_index(**kwargs)

    assert first["created"] == 1
    assert second["created"] == 0
    assert second["updated"] == 0
    assert len(list(account_dir.glob("local_*.json"))) == 2


def test_sync_noop_without_app_index(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(claude_app_index.platform, "system", lambda: "Darwin")

    result = claude_app_index.sync_claude_app_index(
        app_sessions_dir=tmp_path / "missing",
        ledger_path=tmp_path / "ledger.json",
        db_path=tmp_path / "missing.sqlite3",
    )

    assert result == {"supported": False, "reason": "app_index_not_found"}
