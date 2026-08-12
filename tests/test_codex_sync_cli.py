from __future__ import annotations

import importlib

from click.testing import CliRunner

from openbase_coder_cli.thread_sync.thread_exchange import ThreadSnapshotResult
from openbase_coder_cli.thread_sync.thread_import import CodexThreadSyncResult

codex_sync_cli = importlib.import_module("openbase_coder_cli.cli.codex_sync")


def test_codex_sync_once_invokes_sync_pass(monkeypatch) -> None:
    calls = []

    def fake_sync_codex_threads_once(**kwargs):
        calls.append(kwargs)
        return [
            CodexThreadSyncResult(
                "thread-1", "transferred", "normal_to_voice", "synced_to_voice"
            ),
            CodexThreadSyncResult("thread-2", "conflict", None, "both_homes_changed"),
            CodexThreadSyncResult("thread-3", "skipped", None, "skipped_active"),
        ]

    monkeypatch.setattr(
        codex_sync_cli,
        "sync_codex_threads_once",
        fake_sync_codex_threads_once,
    )

    result = CliRunner().invoke(
        codex_sync_cli.codex_sync, ["once", "--stability-delay", "0"]
    )

    assert result.exit_code == 0
    assert calls == [{"stability_delay_seconds": 0.0, "max_age_days": 15}]
    assert "transferred=1 conflicts=1 skipped=1 total=3" in result.output


def test_sync_result_summary_aggregates_status_reason_and_direction_counts() -> None:
    summary = codex_sync_cli._sync_result_summary(
        [
            CodexThreadSyncResult(
                "thread-1", "transferred", "normal_to_voice", "synced_to_voice"
            ),
            CodexThreadSyncResult(
                "thread-2", "transferred", "voice_to_normal", "synced_to_normal"
            ),
            CodexThreadSyncResult("thread-3", "conflict", None, "both_homes_changed"),
            CodexThreadSyncResult("thread-4", "skipped", None, "skipped_old"),
            CodexThreadSyncResult("thread-5", "skipped", None, "skipped_active"),
            CodexThreadSyncResult("thread-6", "already_synced", None, "same_content"),
        ]
    )

    assert summary["total"] == 6
    assert summary["transferred"] == 2
    assert summary["conflicts"] == 1
    assert summary["errors"] == 0
    assert summary["skipped"] == 2
    assert summary["already_synced"] == 1
    assert summary["direction_counts"] == "none:4,normal_to_voice:1,voice_to_normal:1"
    assert (
        summary["reason_counts"]
        == "already_synced:same_content:1,conflict:both_homes_changed:1,"
        "skipped:skipped_active:1,skipped:skipped_old:1,"
        "transferred:synced_to_normal:1,transferred:synced_to_voice:1"
    )


def test_codex_sync_devices_once_invokes_snapshot_sync(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_sync_thread_snapshots_once(**kwargs):
        calls.append(kwargs)
        return {
            "exports": [
                ThreadSnapshotResult("thread-1", "exported", "snapshot_written"),
                ThreadSnapshotResult("thread-2", "skipped", "skipped_active"),
            ],
            "imports": [
                ThreadSnapshotResult("thread-3", "imported", "snapshot_imported"),
                ThreadSnapshotResult("thread-4", "conflict", "divergent_fingerprint"),
            ],
        }

    monkeypatch.setattr(
        codex_sync_cli,
        "sync_thread_snapshots_once",
        fake_sync_thread_snapshots_once,
    )

    result = CliRunner().invoke(
        codex_sync_cli.codex_sync,
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


def test_snapshot_result_summary_aggregates_statuses() -> None:
    summary = codex_sync_cli._snapshot_result_summary(
        [
            ThreadSnapshotResult("thread-1", "exported", "snapshot_written"),
            ThreadSnapshotResult("thread-2", "imported", "snapshot_imported"),
            ThreadSnapshotResult("thread-3", "conflict", "divergent_fingerprint"),
            ThreadSnapshotResult("thread-4", "already_imported", "fingerprint_seen"),
        ]
    )

    assert summary["total"] == 4
    assert summary["exported"] == 1
    assert summary["imported"] == 1
    assert summary["conflicts"] == 1
    assert summary["already_imported"] == 1
