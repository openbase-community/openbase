"""Claude thread sync ledger I/O (home-pair and cross-device ledgers)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .thread_sync_common import (
    read_device_ledger,
    read_scoped_ledger,
    record_sync_conflict,
    record_synced_pair,
    write_json_atomic,
    write_scoped_ledger,
)

# Preserve the historical log record name so malformed-ledger warnings keep
# surfacing under the claude_thread_sync logger that operators filter on.
logger = logging.getLogger("openbase_coder_cli.thread_sync.claude_thread_sync")


def _read_sync_ledger(path: Path) -> dict[str, Any]:
    return read_scoped_ledger(
        path,
        scope_key="sessions",
        logger=logger,
        malformed_event="claude_thread_sync event=ledger_malformed",
    )


def _write_sync_ledger(path: Path, ledger: dict[str, Any]) -> None:
    write_scoped_ledger(path, scope_key="sessions", ledger=ledger)


def _read_device_ledger(path: Path) -> dict[str, Any]:
    return read_device_ledger(
        path,
        scope_key="sessions",
        logger=logger,
        malformed_event="claude_thread_device_sync event=ledger_malformed",
    )


def _write_device_ledger(path: Path, ledger: dict[str, Any]) -> None:
    write_json_atomic(path, ledger)


def _record_synced_pair(
    ledger: dict[str, Any],
    session_id: str,
    normal_fingerprint: dict[str, Any],
    openbase_fingerprint: dict[str, Any],
    reason: str,
) -> None:
    record_synced_pair(
        ledger,
        entity_key="session_id",
        entity_id=session_id,
        left_key="normal",
        left_fingerprint=normal_fingerprint,
        right_key="openbase",
        right_fingerprint=openbase_fingerprint,
        reason=reason,
    )


def _record_conflict(
    ledger: dict[str, Any],
    session_id: str,
    normal_fingerprint: dict[str, Any],
    openbase_fingerprint: dict[str, Any],
    reason: str,
) -> None:
    record_sync_conflict(
        ledger,
        entity_key="session_id",
        entity_id=session_id,
        left_key="normal",
        left_fingerprint=normal_fingerprint,
        right_key="openbase",
        right_fingerprint=openbase_fingerprint,
        reason=reason,
    )
