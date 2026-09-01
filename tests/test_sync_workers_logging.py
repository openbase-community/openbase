from __future__ import annotations

import importlib
import logging

from openbase_coder_cli.code_sync import reconciler

# The cli package re-exports a click group named sync_workers; import the
# module itself to reach the tick functions.
sync_workers = importlib.import_module("openbase_coder_cli.cli.sync_workers")


def test_code_sync_tick_logs_full_counts(monkeypatch, caplog) -> None:
    summary = {
        "repos": [
            {"action": "fast_forwarded"},
            {"action": "up_to_date"},
            {"action": "awaiting_files"},
        ],
        "repository_manifests": [{"path": "", "action": "converged"}],
        "errors": ["cs-x/alpha: TimeoutExpired: git timed out"],
        "conflicts_count": 2,
        "lease": {"action": "noop"},
    }
    monkeypatch.setattr(reconciler, "run_tick_if_enabled", lambda: summary)

    with caplog.at_level(logging.INFO):
        sync_workers._code_sync_reconcile_tick()

    messages = " | ".join(record.getMessage() for record in caplog.records)
    assert "code_sync tick_complete" in messages
    assert "fast_forwarded=1" in messages
    assert "up_to_date=1" in messages
    assert "awaiting_files=1" in messages
    assert "converged=1" in messages
    assert "conflicts=2" in messages
    assert "errors=1" in messages
    assert "lease=noop" in messages
    assert "tick_errors" in messages
    assert "cs-x/alpha" in messages


def test_code_sync_tick_silent_when_disabled(monkeypatch, caplog) -> None:
    monkeypatch.setattr(reconciler, "run_tick_if_enabled", lambda: None)

    with caplog.at_level(logging.INFO):
        sync_workers._code_sync_reconcile_tick()

    assert all(
        "tick_complete" not in record.getMessage() for record in caplog.records
    )
