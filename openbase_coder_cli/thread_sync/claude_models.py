"""Shared constants, dataclasses, and errors for Claude thread sync."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openbase_coder_cli.paths import OPENBASE_BASE_DIR

SCHEMA_VERSION = 1
CLAUDE_SYNC_LEDGER_NAME = "claude-thread-sync-ledger.json"
CLAUDE_DEVICE_LEDGER_NAME = "claude-thread-device-sync-ledger.json"
# Shared with codex: one transported product-state exchange folder
# carries both backends (importers skip the other backend's snapshots).
DEFAULT_DEVICE_EXCHANGE_DIR = OPENBASE_BASE_DIR / "thread-sync"
DEFAULT_DEVICE_LEDGER_PATH = OPENBASE_BASE_DIR / CLAUDE_DEVICE_LEDGER_NAME
DEFAULT_SYNC_LEDGER_PATH = OPENBASE_BASE_DIR / CLAUDE_SYNC_LEDGER_NAME
DEFAULT_SYNC_MAX_AGE_DAYS = 15
IMPORT_STAGING_DIR_NAME = ".claude-thread-sync-staging"
IMPORT_BACKUP_DIR_NAME = ".claude-thread-sync-backups"
DEFAULT_LEGACY_SUPER_AGENTS_STATE_PATH = Path.home() / ".super-agents" / "state.json"
CLAUDE_EVENT_TYPES = {
    "assistant",
    "attachment",
    "file-history-snapshot",
    "last-prompt",
    "permission-mode",
    "queue-operation",
    "system",
    "user",
}
FINGERPRINT_MATCH_KEYS = ("root_sha256", "root_size", "tree_sha256")


@dataclass(frozen=True)
class ClaudeSessionSnapshot:
    session_id: str
    project_key: str
    root_path: Path
    relative_root: Path
    cwd: str | None
    name: str
    latest_assistant_message: str | None
    created_at_ms: int | None
    updated_at_ms: int
    fingerprint: dict[str, Any]


@dataclass(frozen=True)
class ClaudeThreadSyncResult:
    session_id: str
    status: str
    direction: str | None
    reason: str
    source_path: str | None = None
    target_path: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "direction": self.direction,
            "reason": self.reason,
            "source_path": self.source_path,
            "target_path": self.target_path,
        }


@dataclass(frozen=True)
class ClaudeThreadSnapshotResult:
    session_id: str
    status: str
    reason: str
    snapshot_path: str | None = None
    fingerprint: str | None = None
    source_device_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "reason": self.reason,
            "snapshot_path": self.snapshot_path,
            "fingerprint": self.fingerprint,
            "source_device_id": self.source_device_id,
        }


class ClaudeConflictResolutionError(ValueError):
    """Raised when a Claude session sync conflict cannot be resolved."""
