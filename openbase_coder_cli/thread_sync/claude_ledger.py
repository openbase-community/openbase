"""Claude cross-device thread sync ledger I/O."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .thread_sync_common import (
    read_device_ledger,
    write_json_atomic,
)

# Preserve the historical log record name so malformed-ledger warnings keep
# surfacing under the claude_thread_sync logger that operators filter on.
logger = logging.getLogger("openbase_coder_cli.thread_sync.claude_thread_sync")


def _read_device_ledger(path: Path) -> dict[str, Any]:
    return read_device_ledger(
        path,
        scope_key="sessions",
        logger=logger,
        malformed_event="claude_thread_device_sync event=ledger_malformed",
    )


def _write_device_ledger(path: Path, ledger: dict[str, Any]) -> None:
    write_json_atomic(path, ledger)
