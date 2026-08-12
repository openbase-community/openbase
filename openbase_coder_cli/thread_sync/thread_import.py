"""Copy and sync Codex threads between the normal and Openbase voice Codex homes."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openbase_coder_cli.paths import (
    CODEX_HOME_DIR,
    NORMAL_CODEX_HOME_DIR,
    OPENBASE_BASE_DIR,
)

from .thread_sync_common import (
    ledger_sync_decision,
    path_stable,
    read_scoped_ledger,
    record_sync_conflict,
    record_synced_pair,
    super_agents_state_db_path,
    sync_cutoff_ms,
    write_scoped_ledger,
)

STATE_DB_NAME = "state_5.sqlite"
SESSION_INDEX_NAME = "session_index.jsonl"
SYNC_LEDGER_NAME = "codex-thread-sync-ledger.json"
TERMINAL_EVENT_TYPES = {"task_complete", "turn_aborted"}
DEFAULT_SUPER_AGENTS_STATE_PATH = Path.home() / ".super-agents" / "state.json"
DEFAULT_SYNC_MAX_AGE_DAYS = 15
FINGERPRINT_MATCH_KEYS = (
    "rollout_sha256",
    "rollout_size",
    "updated_at_ms",
    "updated_at",
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexThreadTransferCandidate:
    thread_id: str
    title: str
    updated_at: str | None
    cwd: str | None
    model: str | None
    rollout_path: str | None
    transferred: bool

    @property
    def imported(self) -> bool:
        return self.transferred

    @property
    def exported(self) -> bool:
        return self.transferred

    def to_json(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "updated_at": self.updated_at,
            "cwd": self.cwd,
            "model": self.model,
            "rollout_path": self.rollout_path,
            "transferred": self.transferred,
            "imported": self.transferred,
            "exported": self.transferred,
        }


@dataclass(frozen=True)
class ThreadTransferResult:
    thread_id: str
    transferred: bool
    reason: str
    source_rollout_path: str | None = None
    target_rollout_path: str | None = None

    @property
    def imported(self) -> bool:
        return self.transferred

    @property
    def exported(self) -> bool:
        return self.transferred

    def to_json(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "transferred": self.transferred,
            "imported": self.transferred,
            "exported": self.transferred,
            "reason": self.reason,
            "source_rollout_path": self.source_rollout_path,
            "target_rollout_path": self.target_rollout_path,
        }


class ThreadTransferError(RuntimeError):
    """Raised when a thread cannot be transferred conservatively."""


@dataclass(frozen=True)
class CodexThreadSyncResult:
    thread_id: str
    status: str
    direction: str | None
    reason: str
    source_rollout_path: str | None = None
    target_rollout_path: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "status": self.status,
            "direction": self.direction,
            "reason": self.reason,
            "source_rollout_path": self.source_rollout_path,
            "target_rollout_path": self.target_rollout_path,
        }


@dataclass(frozen=True)
class ThreadSyncSafety:
    safe: bool
    reason: str


NormalCodexThread = CodexThreadTransferCandidate
VoiceCodexThread = CodexThreadTransferCandidate
ThreadImportResult = ThreadTransferResult
ThreadImportError = ThreadTransferError


def list_normal_codex_threads(
    *,
    normal_home: Path = NORMAL_CODEX_HOME_DIR,
    voice_home: Path = CODEX_HOME_DIR,
    limit: int = 50,
    search: str | None = None,
    include_imported: bool = True,
) -> list[NormalCodexThread]:
    """List normal Codex threads that can be imported into the voice home."""
    return list_codex_threads_for_transfer(
        source_home=normal_home,
        target_home=voice_home,
        limit=limit,
        search=search,
        include_transferred=include_imported,
    )


def list_voice_codex_threads(
    *,
    normal_home: Path = NORMAL_CODEX_HOME_DIR,
    voice_home: Path = CODEX_HOME_DIR,
    limit: int = 50,
    search: str | None = None,
    include_exported: bool = True,
) -> list[VoiceCodexThread]:
    """List voice Codex threads that can be exported into the normal home."""
    return list_codex_threads_for_transfer(
        source_home=voice_home,
        target_home=normal_home,
        limit=limit,
        search=search,
        include_transferred=include_exported,
    )


def list_codex_threads_for_transfer(
    *,
    source_home: Path,
    target_home: Path,
    limit: int = 50,
    search: str | None = None,
    include_transferred: bool = True,
) -> list[CodexThreadTransferCandidate]:
    """List Codex threads in one home and mark whether they exist in another."""
    source_db = source_home / STATE_DB_NAME
    index_by_id = _latest_session_index_entries(source_home / SESSION_INDEX_NAME)
    target_thread_ids = _target_thread_ids(target_home / STATE_DB_NAME)
    rows = _thread_rows(source_db)

    threads: list[CodexThreadTransferCandidate] = []
    search_lower = search.lower() if search else None
    for row in rows:
        thread_id = _string(row.get("id"))
        if not thread_id:
            continue
        index_entry = index_by_id.get(thread_id, {})
        title = (
            _string(index_entry.get("thread_name"))
            or _string(row.get("title"))
            or _string(row.get("preview"))
            or thread_id
        )
        cwd = _string(row.get("cwd"))
        transferred = thread_id in target_thread_ids
        haystack = " ".join(
            value for value in (thread_id, title, cwd or "") if value
        ).lower()
        if search_lower and search_lower not in haystack:
            continue
        if transferred and not include_transferred:
            continue
        threads.append(
            CodexThreadTransferCandidate(
                thread_id=thread_id,
                title=title,
                updated_at=_string(index_entry.get("updated_at"))
                or _timestamp_to_iso(row.get("updated_at_ms"))
                or _timestamp_to_iso(row.get("updated_at")),
                cwd=cwd,
                model=_string(row.get("model")),
                rollout_path=_string(row.get("rollout_path")),
                transferred=transferred,
            )
        )

    return threads[: max(limit, 0)]


def import_normal_codex_threads(
    thread_ids: list[str],
    *,
    normal_home: Path = NORMAL_CODEX_HOME_DIR,
    voice_home: Path = CODEX_HOME_DIR,
    overwrite: bool = False,
) -> list[ThreadImportResult]:
    """Copy one or more normal Codex threads into the voice Codex home."""
    return transfer_codex_threads(
        thread_ids,
        source_home=normal_home,
        target_home=voice_home,
        source_label="Normal Codex",
        target_label="Voice Codex",
        success_reason="imported",
        existing_reason="already_imported",
        overwrite=overwrite,
    )


def export_voice_codex_threads(
    thread_ids: list[str],
    *,
    normal_home: Path = NORMAL_CODEX_HOME_DIR,
    voice_home: Path = CODEX_HOME_DIR,
    overwrite: bool = False,
) -> list[ThreadTransferResult]:
    """Copy one or more voice Codex threads into the normal Codex home."""
    return transfer_codex_threads(
        thread_ids,
        source_home=voice_home,
        target_home=normal_home,
        source_label="Voice Codex",
        target_label="Normal Codex",
        success_reason="exported",
        existing_reason="already_exported",
        overwrite=overwrite,
    )


def sync_codex_threads_once(
    *,
    normal_home: Path = NORMAL_CODEX_HOME_DIR,
    voice_home: Path = CODEX_HOME_DIR,
    ledger_path: Path = OPENBASE_BASE_DIR / SYNC_LEDGER_NAME,
    stability_delay_seconds: float = 0.2,
    max_age_days: int | None = DEFAULT_SYNC_MAX_AGE_DAYS,
    active_thread_ids: set[str] | None = None,
) -> list[CodexThreadSyncResult]:
    """Run one conservative bidirectional sync pass between Codex homes."""
    normal_db = normal_home / STATE_DB_NAME
    voice_db = voice_home / STATE_DB_NAME
    if not normal_db.exists():
        raise ThreadTransferError(f"Normal Codex state database not found: {normal_db}")
    if not voice_db.exists():
        raise ThreadTransferError(f"Voice Codex state database not found: {voice_db}")

    active_ids = set(active_thread_ids or set()) | _active_super_agent_thread_ids()
    ledger = _read_sync_ledger(ledger_path)
    normal_rows = {
        row["id"]: row for row in _thread_rows(normal_db) if _string(row.get("id"))
    }
    voice_rows = {
        row["id"]: row for row in _thread_rows(voice_db) if _string(row.get("id"))
    }
    cutoff_ms = sync_cutoff_ms(max_age_days)

    results: list[CodexThreadSyncResult] = []
    for thread_id in _thread_ids_by_updated_at(normal_rows, voice_rows):
        normal_row = normal_rows.get(thread_id)
        voice_row = voice_rows.get(thread_id)
        try:
            if (
                cutoff_ms is not None
                and _thread_latest_updated_ms(normal_row, voice_row) < cutoff_ms
            ):
                result = CodexThreadSyncResult(
                    thread_id, "skipped", None, "skipped_old"
                )
            else:
                result = _sync_one_thread(
                    thread_id,
                    normal_row=normal_row,
                    voice_row=voice_row,
                    normal_home=normal_home,
                    voice_home=voice_home,
                    normal_db=normal_db,
                    voice_db=voice_db,
                    ledger=ledger,
                    stability_delay_seconds=stability_delay_seconds,
                    active_thread_ids=active_ids,
                )
        except Exception as exc:
            result = CodexThreadSyncResult(thread_id, "error", None, type(exc).__name__)
            logger.exception("codex_thread_sync event=error thread_id=%s", thread_id)
        else:
            _log_sync_result(result)
        results.append(result)

    _write_sync_ledger(ledger_path, ledger)
    return results


def transfer_codex_threads(
    thread_ids: list[str],
    *,
    source_home: Path,
    target_home: Path,
    source_label: str = "Source Codex",
    target_label: str = "Target Codex",
    success_reason: str = "transferred",
    existing_reason: str = "already_transferred",
    overwrite: bool = False,
) -> list[ThreadTransferResult]:
    """Copy one or more Codex threads between Codex home directories."""
    if not thread_ids:
        raise ValueError("thread_ids must not be empty")

    source_db = source_home / STATE_DB_NAME
    target_db = target_home / STATE_DB_NAME
    if not source_db.exists():
        raise ThreadTransferError(
            f"{source_label} state database not found: {source_db}"
        )
    if not target_db.exists():
        raise ThreadTransferError(
            f"{target_label} state database not found: {target_db}"
        )

    results: list[ThreadTransferResult] = []
    for thread_id in dict.fromkeys(thread_ids):
        results.append(
            _transfer_one_thread(
                thread_id,
                source_home=source_home,
                target_home=target_home,
                source_db=source_db,
                target_db=target_db,
                overwrite=overwrite,
                success_reason=success_reason,
                existing_reason=existing_reason,
            )
        )
    return results


def _sync_one_thread(
    thread_id: str,
    *,
    normal_row: dict[str, Any] | None,
    voice_row: dict[str, Any] | None,
    normal_home: Path,
    voice_home: Path,
    normal_db: Path,
    voice_db: Path,
    ledger: dict[str, Any],
    stability_delay_seconds: float,
    active_thread_ids: set[str],
) -> CodexThreadSyncResult:
    if thread_id in active_thread_ids:
        return CodexThreadSyncResult(thread_id, "skipped", None, "skipped_active")

    if normal_row is not None and voice_row is None:
        return _sync_direction(
            thread_id,
            source_row=normal_row,
            target_row=None,
            source_home=normal_home,
            target_home=voice_home,
            source_db=normal_db,
            target_db=voice_db,
            direction="normal_to_voice",
            success_reason="synced_to_voice",
            ledger=ledger,
            stability_delay_seconds=stability_delay_seconds,
            overwrite=False,
        )
    if voice_row is not None and normal_row is None:
        return _sync_direction(
            thread_id,
            source_row=voice_row,
            target_row=None,
            source_home=voice_home,
            target_home=normal_home,
            source_db=voice_db,
            target_db=normal_db,
            direction="voice_to_normal",
            success_reason="synced_to_normal",
            ledger=ledger,
            stability_delay_seconds=stability_delay_seconds,
            overwrite=False,
        )
    if normal_row is None or voice_row is None:
        return CodexThreadSyncResult(thread_id, "skipped", None, "not_found")

    normal_fp = _thread_fingerprint(normal_row, normal_home, thread_id)
    voice_fp = _thread_fingerprint(voice_row, voice_home, thread_id)
    if normal_fp is None or voice_fp is None:
        return CodexThreadSyncResult(thread_id, "skipped", None, "rollout_not_found")

    if normal_fp["rollout_sha256"] == voice_fp["rollout_sha256"]:
        _record_synced_pair(ledger, thread_id, normal_fp, voice_fp, "same_content")
        return CodexThreadSyncResult(thread_id, "already_synced", None, "same_content")

    append_only_result = _sync_append_only_prefix_conflict(
        thread_id,
        normal_row=normal_row,
        voice_row=voice_row,
        normal_home=normal_home,
        voice_home=voice_home,
        normal_db=normal_db,
        voice_db=voice_db,
        normal_fp=normal_fp,
        voice_fp=voice_fp,
        ledger=ledger,
        stability_delay_seconds=stability_delay_seconds,
    )
    if append_only_result is not None:
        return append_only_result

    decision = ledger_sync_decision(
        ledger.get(thread_id),
        left_key="normal",
        right_key="voice",
        left_fingerprint=normal_fp,
        right_fingerprint=voice_fp,
        fingerprint_keys=FINGERPRINT_MATCH_KEYS,
    )
    if decision in {"both_changed", "conflict_unresolved"}:
        reason = (
            "both_homes_changed"
            if decision == "both_changed"
            else "conflict_unresolved"
        )
        _record_conflict(ledger, thread_id, normal_fp, voice_fp, reason)
        return CodexThreadSyncResult(thread_id, "conflict", None, reason)
    if decision == "left_changed":
        if not _target_row_safe_for_overwrite(voice_row, voice_home, thread_id):
            return CodexThreadSyncResult(
                thread_id, "skipped", "normal_to_voice", "target_active"
            )
        return _sync_direction(
            thread_id,
            source_row=normal_row,
            target_row=voice_row,
            source_home=normal_home,
            target_home=voice_home,
            source_db=normal_db,
            target_db=voice_db,
            direction="normal_to_voice",
            success_reason="synced_to_voice",
            ledger=ledger,
            stability_delay_seconds=stability_delay_seconds,
            overwrite=True,
        )
    if decision == "right_changed":
        if not _target_row_safe_for_overwrite(normal_row, normal_home, thread_id):
            return CodexThreadSyncResult(
                thread_id, "skipped", "voice_to_normal", "target_active"
            )
        return _sync_direction(
            thread_id,
            source_row=voice_row,
            target_row=normal_row,
            source_home=voice_home,
            target_home=normal_home,
            source_db=voice_db,
            target_db=normal_db,
            direction="voice_to_normal",
            success_reason="synced_to_normal",
            ledger=ledger,
            stability_delay_seconds=stability_delay_seconds,
            overwrite=True,
        )
    return CodexThreadSyncResult(thread_id, "already_synced", None, "ledger_current")


def _sync_append_only_prefix_conflict(
    thread_id: str,
    *,
    normal_row: dict[str, Any],
    voice_row: dict[str, Any],
    normal_home: Path,
    voice_home: Path,
    normal_db: Path,
    voice_db: Path,
    normal_fp: dict[str, Any],
    voice_fp: dict[str, Any],
    ledger: dict[str, Any],
    stability_delay_seconds: float,
) -> CodexThreadSyncResult | None:
    normal_size = int(normal_fp.get("rollout_size") or 0)
    voice_size = int(voice_fp.get("rollout_size") or 0)
    if normal_size == voice_size:
        return None

    normal_rollout = _source_rollout_path(normal_row, normal_home, thread_id)
    voice_rollout = _source_rollout_path(voice_row, voice_home, thread_id)
    if normal_rollout is None or voice_rollout is None:
        return None

    if normal_size > voice_size:
        if not _rollout_has_prefix(normal_rollout, voice_rollout):
            return None
        if not _target_row_safe_for_overwrite(voice_row, voice_home, thread_id):
            return CodexThreadSyncResult(
                thread_id, "skipped", "normal_to_voice", "target_active"
            )
        return _sync_direction(
            thread_id,
            source_row=normal_row,
            target_row=voice_row,
            source_home=normal_home,
            target_home=voice_home,
            source_db=normal_db,
            target_db=voice_db,
            direction="normal_to_voice",
            success_reason="synced_append_only_to_voice",
            ledger=ledger,
            stability_delay_seconds=stability_delay_seconds,
            overwrite=True,
        )

    if not _rollout_has_prefix(voice_rollout, normal_rollout):
        return None
    if not _target_row_safe_for_overwrite(normal_row, normal_home, thread_id):
        return CodexThreadSyncResult(
            thread_id, "skipped", "voice_to_normal", "target_active"
        )
    return _sync_direction(
        thread_id,
        source_row=voice_row,
        target_row=normal_row,
        source_home=voice_home,
        target_home=normal_home,
        source_db=voice_db,
        target_db=normal_db,
        direction="voice_to_normal",
        success_reason="synced_append_only_to_normal",
        ledger=ledger,
        stability_delay_seconds=stability_delay_seconds,
        overwrite=True,
    )


def _sync_direction(
    thread_id: str,
    *,
    source_row: dict[str, Any],
    target_row: dict[str, Any] | None,
    source_home: Path,
    target_home: Path,
    source_db: Path,
    target_db: Path,
    direction: str,
    success_reason: str,
    ledger: dict[str, Any],
    stability_delay_seconds: float,
    overwrite: bool,
) -> CodexThreadSyncResult:
    source_safety = _thread_safe_for_sync(
        source_row,
        source_home,
        thread_id,
        stability_delay_seconds=stability_delay_seconds,
    )
    if not source_safety.safe:
        return CodexThreadSyncResult(
            thread_id, "skipped", direction, source_safety.reason
        )

    transfer = _transfer_one_thread(
        thread_id,
        source_home=source_home,
        target_home=target_home,
        source_db=source_db,
        target_db=target_db,
        overwrite=overwrite,
        success_reason=success_reason,
        existing_reason="already_synced",
    )
    if not transfer.transferred:
        return CodexThreadSyncResult(
            thread_id,
            "skipped",
            direction,
            transfer.reason,
            transfer.source_rollout_path,
            transfer.target_rollout_path,
        )

    source_fp = _thread_fingerprint(source_row, source_home, thread_id)
    target_fp = source_fp
    if source_fp is not None and target_fp is not None:
        if direction == "normal_to_voice":
            _record_synced_pair(ledger, thread_id, source_fp, target_fp, success_reason)
        else:
            _record_synced_pair(ledger, thread_id, target_fp, source_fp, success_reason)

    return CodexThreadSyncResult(
        thread_id,
        "transferred",
        direction,
        success_reason,
        transfer.source_rollout_path,
        transfer.target_rollout_path,
    )


def _transfer_one_thread(
    thread_id: str,
    *,
    source_home: Path,
    target_home: Path,
    source_db: Path,
    target_db: Path,
    overwrite: bool,
    success_reason: str,
    existing_reason: str,
) -> ThreadTransferResult:
    source_row = _thread_row(source_db, thread_id)
    if source_row is None:
        return ThreadTransferResult(thread_id, False, "not_found")
    if _thread_row(target_db, thread_id) is not None and not overwrite:
        return ThreadTransferResult(thread_id, False, existing_reason)

    source_rollout = _source_rollout_path(source_row, source_home, thread_id)
    if source_rollout is None:
        return ThreadTransferResult(thread_id, False, "rollout_not_found")
    target_rollout = _target_rollout_path(source_rollout, source_home, target_home)

    target_rollout.parent.mkdir(parents=True, exist_ok=True)
    if target_rollout.exists() and not overwrite:
        return ThreadTransferResult(
            thread_id,
            False,
            "target_rollout_exists",
            str(source_rollout),
            str(target_rollout),
        )
    shutil.copy2(source_rollout, target_rollout)

    _copy_thread_state_row(
        source_db,
        target_db,
        "threads",
        thread_id,
        overrides={"rollout_path": str(target_rollout)},
        overwrite=overwrite,
    )
    _copy_thread_dynamic_tools(source_db, target_db, thread_id, overwrite=overwrite)
    _append_session_index_entry(
        thread_id,
        source_index=source_home / SESSION_INDEX_NAME,
        target_index=target_home / SESSION_INDEX_NAME,
        fallback_title=_string(source_row.get("title")) or thread_id,
        fallback_updated_at=_timestamp_to_iso(source_row.get("updated_at_ms"))
        or _timestamp_to_iso(source_row.get("updated_at")),
        overwrite=overwrite,
    )

    return ThreadTransferResult(
        thread_id,
        True,
        success_reason,
        str(source_rollout),
        str(target_rollout),
    )


def _thread_ids_by_updated_at(
    normal_rows: dict[str, dict[str, Any]],
    voice_rows: dict[str, dict[str, Any]],
) -> list[str]:
    def updated_at(thread_id: str) -> int:
        values = []
        for rows in (normal_rows, voice_rows):
            row = rows.get(thread_id)
            if row:
                value = row.get("updated_at_ms") or row.get("updated_at") or 0
                values.append(value if isinstance(value, int) else 0)
        return max(values or [0])

    return sorted(set(normal_rows) | set(voice_rows), key=updated_at, reverse=True)


def _thread_latest_updated_ms(
    *rows: dict[str, Any] | None,
) -> int:
    values = [_row_updated_ms(row) for row in rows if row is not None]
    return max(values or [0])


def _row_updated_ms(row: dict[str, Any]) -> int:
    updated_at_ms = row.get("updated_at_ms")
    if isinstance(updated_at_ms, int):
        return updated_at_ms
    updated_at = row.get("updated_at")
    if isinstance(updated_at, int):
        return updated_at * 1000
    return 0


def _thread_safe_for_sync(
    row: dict[str, Any],
    home: Path,
    thread_id: str,
    *,
    stability_delay_seconds: float,
) -> ThreadSyncSafety:
    rollout = _source_rollout_path(row, home, thread_id)
    if rollout is None:
        return ThreadSyncSafety(False, "rollout_not_found")
    if not path_stable(rollout, stability_delay_seconds):
        return ThreadSyncSafety(False, "skipped_unstable")
    terminal = _rollout_terminal_event(rollout)
    if terminal is None:
        return ThreadSyncSafety(False, "rollout_malformed")
    if terminal not in TERMINAL_EVENT_TYPES:
        if _rollout_open_for_write(rollout):
            return ThreadSyncSafety(False, "skipped_active")
        return ThreadSyncSafety(False, "non_terminal")
    return ThreadSyncSafety(True, "safe")


def _target_row_safe_for_overwrite(
    row: dict[str, Any],
    home: Path,
    thread_id: str,
) -> bool:
    rollout = _source_rollout_path(row, home, thread_id)
    return (
        rollout is not None
        and not _rollout_open_for_write(rollout)
        and _rollout_terminal_event(rollout) in TERMINAL_EVENT_TYPES
    )


def _rollout_terminal_event(path: Path) -> str | None:
    last_event_type: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        event_type = _string(event.get("type"))
        payload = event.get("payload")
        if event_type == "event_msg" and isinstance(payload, dict):
            last_event_type = _string(payload.get("type"))
        elif event_type:
            last_event_type = event_type
    return last_event_type


def _rollout_open_for_write(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["lsof", "-nP", "--", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        fd = parts[3]
        if "w" in fd or "u" in fd:
            return True
    return False


def _rollout_has_prefix(candidate: Path, prefix: Path) -> bool:
    try:
        candidate_size = candidate.stat().st_size
        prefix_size = prefix.stat().st_size
    except OSError:
        return False
    if prefix_size > candidate_size:
        return False

    with candidate.open("rb") as candidate_handle, prefix.open("rb") as prefix_handle:
        for prefix_chunk in iter(lambda: prefix_handle.read(1024 * 1024), b""):
            if candidate_handle.read(len(prefix_chunk)) != prefix_chunk:
                return False
    return True


def _thread_fingerprint(
    row: dict[str, Any],
    home: Path,
    thread_id: str,
) -> dict[str, Any] | None:
    rollout = _source_rollout_path(row, home, thread_id)
    return _fingerprint_from_rollout_path(rollout, row)


def _fingerprint_from_rollout_path(
    rollout: Path | None,
    row: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if rollout is None or not rollout.exists():
        return None
    digest = hashlib.sha256()
    with rollout.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = rollout.stat()
    return {
        "rollout_sha256": digest.hexdigest(),
        "rollout_size": stat.st_size,
        "updated_at_ms": row.get("updated_at_ms") if row else None,
        "updated_at": row.get("updated_at") if row else None,
    }


def _record_synced_pair(
    ledger: dict[str, Any],
    thread_id: str,
    normal_fingerprint: dict[str, Any],
    voice_fingerprint: dict[str, Any],
    reason: str,
) -> None:
    record_synced_pair(
        ledger,
        entity_key="thread_id",
        entity_id=thread_id,
        left_key="normal",
        left_fingerprint=normal_fingerprint,
        right_key="voice",
        right_fingerprint=voice_fingerprint,
        reason=reason,
    )


def _record_conflict(
    ledger: dict[str, Any],
    thread_id: str,
    normal_fingerprint: dict[str, Any],
    voice_fingerprint: dict[str, Any],
    reason: str,
) -> None:
    record_sync_conflict(
        ledger,
        entity_key="thread_id",
        entity_id=thread_id,
        left_key="normal",
        left_fingerprint=normal_fingerprint,
        right_key="voice",
        right_fingerprint=voice_fingerprint,
        reason=reason,
    )


def _read_sync_ledger(path: Path) -> dict[str, Any]:
    return read_scoped_ledger(
        path,
        scope_key="threads",
        logger=logger,
        malformed_event="codex_thread_sync event=ledger_malformed",
    )


def _write_sync_ledger(path: Path, ledger: dict[str, Any]) -> None:
    write_scoped_ledger(path, scope_key="threads", ledger=ledger)


def _active_super_agent_thread_ids(
    state_path: Path = DEFAULT_SUPER_AGENTS_STATE_PATH,
    *,
    db_path: Path | None = None,
) -> set[str]:
    active = _active_super_agent_thread_ids_from_db(db_path)
    if not state_path.exists():
        return active
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return active
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    if not isinstance(sessions, dict):
        return active
    for key, value in sessions.items():
        if not isinstance(value, dict):
            continue
        status = value.get("lastStatus")
        active_turn = value.get("activeTurnId")
        thread_id = _string(value.get("threadId")) or (
            key if isinstance(key, str) else None
        )
        if thread_id and status in {"running", "waiting"} and active_turn:
            active.add(thread_id)
    return active


def _active_super_agent_thread_ids_from_db(db_path: Path | None = None) -> set[str]:
    """Read active Super Agents thread ids from the SQLite agent store."""
    resolved_db = db_path or super_agents_state_db_path()
    if not resolved_db.exists():
        return set()
    with closing(sqlite3.connect(resolved_db)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                select id, backend_session_id from sessions
                where status = 'running' or active_turn_id is not null
                """
            ).fetchall()
        except sqlite3.Error:
            rows = []
    active: set[str] = set()
    for row in rows:
        thread_id = _string(row["backend_session_id"]) or _thread_id_from_store_id(
            _string(row["id"])
        )
        if thread_id:
            active.add(thread_id)
    return active


def _thread_id_from_store_id(store_id: str | None) -> str | None:
    """Recover a Codex thread UUID from a prefixed agent-store session id."""
    if not store_id or not store_id.startswith("codex_"):
        return None
    try:
        return str(uuid.UUID(hex=store_id.removeprefix("codex_")))
    except ValueError:
        return None


def _log_sync_result(result: CodexThreadSyncResult) -> None:
    if not _should_log_sync_result(result):
        return

    message = (
        "codex_thread_sync event=%s thread_id=%s direction=%s reason=%s "
        "source_rollout=%s target_rollout=%s"
    )
    args = (
        result.status,
        result.thread_id,
        result.direction,
        result.reason,
        result.source_rollout_path,
        result.target_rollout_path,
    )
    if result.status == "conflict":
        logger.warning(message, *args)
    elif result.status == "error":
        logger.error(message, *args)
    else:
        logger.info(message, *args)


def _should_log_sync_result(result: CodexThreadSyncResult) -> bool:
    if result.status in {"transferred", "conflict", "error"}:
        return True
    if result.status != "skipped":
        return False
    if result.reason == "skipped_active":
        return True
    return result.reason not in {
        "skipped_old",
        "non_terminal",
        "same_content",
        "ledger_current",
    }


def _thread_rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    with _managed_connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM threads WHERE archived = 0 ORDER BY updated_at_ms DESC, updated_at DESC"
            )
        ]


def _thread_row(db_path: Path, thread_id: str) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    with _managed_connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _target_thread_ids(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    with _managed_connect(db_path) as conn:
        rows = conn.execute("SELECT id FROM threads").fetchall()
    return {row[0] for row in rows if isinstance(row[0], str) and row[0]}


def _copy_thread_state_row(
    source_db: Path,
    target_db: Path,
    table: str,
    thread_id: str,
    *,
    overrides: dict[str, Any] | None = None,
    overwrite: bool,
) -> None:
    with (
        _managed_connect(source_db) as source_conn,
        _managed_connect(target_db) as target_conn,
    ):
        source_conn.row_factory = sqlite3.Row
        target_conn.row_factory = sqlite3.Row
        source_columns = _table_columns(source_conn, table)
        target_columns = _table_columns(target_conn, table)
        columns = [column for column in source_columns if column in target_columns]
        row = source_conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            return
        values = dict(row)
        values.update(overrides or {})
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(columns)
        verb = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
        target_conn.execute(
            f"{verb} INTO {table} ({column_sql}) VALUES ({placeholders})",
            [values.get(column) for column in columns],
        )


def _copy_thread_dynamic_tools(
    source_db: Path,
    target_db: Path,
    thread_id: str,
    *,
    overwrite: bool,
) -> None:
    with (
        _managed_connect(source_db) as source_conn,
        _managed_connect(target_db) as target_conn,
    ):
        source_conn.row_factory = sqlite3.Row
        target_conn.row_factory = sqlite3.Row
        if not _has_table(source_conn, "thread_dynamic_tools") or not _has_table(
            target_conn,
            "thread_dynamic_tools",
        ):
            return
        columns = [
            column
            for column in _table_columns(source_conn, "thread_dynamic_tools")
            if column in _table_columns(target_conn, "thread_dynamic_tools")
        ]
        rows = source_conn.execute(
            f"SELECT {', '.join(columns)} FROM thread_dynamic_tools WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
        if overwrite:
            target_conn.execute(
                "DELETE FROM thread_dynamic_tools WHERE thread_id = ?",
                (thread_id,),
            )
        verb = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(columns)
        for row in rows:
            values = dict(row)
            target_conn.execute(
                f"{verb} INTO thread_dynamic_tools ({column_sql}) VALUES ({placeholders})",
                [values.get(column) for column in columns],
            )


def _source_rollout_path(
    row: dict[str, Any],
    normal_home: Path,
    thread_id: str,
) -> Path | None:
    rollout_path = _string(row.get("rollout_path"))
    if rollout_path:
        path = Path(rollout_path).expanduser()
        if path.exists():
            return path
    matches = sorted((normal_home / "sessions").glob(f"**/*{thread_id}.jsonl"))
    return matches[-1] if matches else None


def _target_rollout_path(
    source_rollout: Path, normal_home: Path, voice_home: Path
) -> Path:
    try:
        relative = source_rollout.relative_to(normal_home)
    except ValueError:
        relative = Path("sessions") / source_rollout.name
    return voice_home / relative


def _latest_session_index_entries(index_path: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    if not index_path.exists():
        return entries
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        thread_id = _string(entry.get("id"))
        if thread_id:
            entries[thread_id] = entry
    return entries


def _append_session_index_entry(
    thread_id: str,
    *,
    source_index: Path,
    target_index: Path,
    fallback_title: str,
    fallback_updated_at: str | None,
    overwrite: bool,
) -> None:
    target_entries = _latest_session_index_entries(target_index)
    if thread_id in target_entries and not overwrite:
        return
    source_entry = _latest_session_index_entries(source_index).get(thread_id)
    entry = (
        dict(source_entry)
        if source_entry
        else {
            "id": thread_id,
            "thread_name": fallback_title,
            "updated_at": fallback_updated_at,
        }
    )
    if fallback_updated_at:
        entry["updated_at"] = fallback_updated_at
    target_index.parent.mkdir(parents=True, exist_ok=True)
    with target_index.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, separators=(",", ":")) + "\n")


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


@contextmanager
def _managed_connect(path: Path):
    conn = _connect(path)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp_to_iso(value: Any) -> str | None:
    if not isinstance(value, int):
        return None
    if value > 10_000_000_000:
        seconds = value / 1000
    else:
        seconds = value
    from datetime import UTC, datetime

    return datetime.fromtimestamp(seconds, tz=UTC).isoformat().replace("+00:00", "Z")
