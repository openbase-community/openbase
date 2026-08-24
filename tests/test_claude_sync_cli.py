from __future__ import annotations

import importlib

from click.testing import CliRunner

from openbase_coder_cli.thread_sync.claude_thread_sync import (
    ClaudeThreadSnapshotResult,
)

claude_sync_cli = importlib.import_module("openbase_coder_cli.cli.claude_sync")


def test_claude_sync_devices_once_invokes_snapshot_sync(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_sync_claude_thread_snapshots_once(**kwargs):
        calls.append(kwargs)
        return {
            "exports": [
                ClaudeThreadSnapshotResult("session-1", "exported", "snapshot_written"),
                ClaudeThreadSnapshotResult("session-2", "skipped", "skipped_active"),
            ],
            "imports": [
                ClaudeThreadSnapshotResult("session-3", "imported", "snapshot_imported"),
                ClaudeThreadSnapshotResult("session-4", "conflict", "divergent_fingerprint"),
            ],
        }

    monkeypatch.setattr(
        claude_sync_cli,
        "sync_claude_thread_snapshots_once",
        fake_sync_claude_thread_snapshots_once,
    )

    result = CliRunner().invoke(
        claude_sync_cli.claude_sync,
        [
            "devices",
            "once",
            "--exchange-dir",
            str(tmp_path / "exchange"),
            "--stability-delay",
            "0",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "exchange_dir": tmp_path / "exchange",
            "stability_delay_seconds": 0.0,
            "max_age_days": 15,
        }
    ]
    assert "exported=1 imported=1 conflicts=1 total=4" in result.output


def test_claude_sync_devices_status_prints_conflict_count(monkeypatch, tmp_path) -> None:
    def fake_status(**kwargs):
        return {
            "device": {"device_id": "device-1", "device_name": "laptop"},
            "exchange_dir": str(kwargs["exchange_dir"]),
            "ledger_path": str(tmp_path / "ledger.json"),
            "snapshot_count": 2,
            "session_count": 1,
            "conflict_count": 1,
            "conflicts": [],
        }

    monkeypatch.setattr(claude_sync_cli, "claude_thread_snapshot_status", fake_status)

    result = CliRunner().invoke(
        claude_sync_cli.claude_sync,
        ["devices", "status", "--exchange-dir", str(tmp_path / "exchange")],
    )

    assert result.exit_code == 0
    assert "Device: laptop" in result.output
    assert "Snapshots=2 sessions=1 conflicts=1" in result.output
