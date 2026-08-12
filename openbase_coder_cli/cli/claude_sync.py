from __future__ import annotations

import logging
import time
from collections import Counter
from pathlib import Path

import click

from openbase_coder_cli.thread_sync.claude_thread_sync import (
    DEFAULT_DEVICE_EXCHANGE_DIR,
    ClaudeThreadSnapshotResult,
    ClaudeThreadSyncResult,
    claude_thread_snapshot_status,
    export_claude_thread_snapshots,
    import_claude_thread_snapshots,
    sync_claude_thread_snapshots_once,
    sync_claude_threads_once,
)
from openbase_coder_cli.thread_sync.thread_exchange import get_or_create_device_identity


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(asctime)s %(name)s %(message)s",
    )


def _sync_result_summary(results: list[ClaudeThreadSyncResult]) -> dict[str, str | int]:
    statuses = Counter(result.status for result in results)
    reasons = Counter(f"{result.status}:{result.reason}" for result in results)
    directions = Counter(result.direction or "none" for result in results)
    return {
        "total": len(results),
        "transferred": statuses["transferred"],
        "conflicts": statuses["conflict"],
        "errors": statuses["error"],
        "skipped": statuses["skipped"],
        "already_synced": statuses["already_synced"],
        "reason_counts": _format_counts(reasons),
        "direction_counts": _format_counts(directions),
    }


def _snapshot_result_summary(
    results: list[ClaudeThreadSnapshotResult],
) -> dict[str, str | int]:
    statuses = Counter(result.status for result in results)
    reasons = Counter(f"{result.status}:{result.reason}" for result in results)
    return {
        "total": len(results),
        "exported": statuses["exported"],
        "imported": statuses["imported"],
        "conflicts": statuses["conflict"],
        "skipped": statuses["skipped"],
        "already_exported": statuses["already_exported"],
        "already_imported": statuses["already_imported"],
        "reason_counts": _format_counts(reasons),
    }


def _format_counts(counts: Counter[str]) -> str:
    if not counts:
        return "none"
    return ",".join(f"{key}:{counts[key]}" for key in sorted(counts))


@click.group("claude-sync")
def claude_sync() -> None:
    """Synchronize Claude Code threads between normal and Openbase config homes."""


@claude_sync.command("once")
@click.option("--stability-delay", default=0.2, show_default=True, type=float)
@click.option("--max-age-days", default=15, show_default=True, type=int)
@click.option("--ledger", type=click.Path(path_type=Path), default=None)
@click.option("--verbose", is_flag=True)
def once(
    stability_delay: float,
    max_age_days: int,
    ledger: Path | None,
    verbose: bool,
) -> None:
    """Run one Claude Code thread sync pass."""
    _configure_logging(verbose)
    kwargs = {
        "stability_delay_seconds": max(stability_delay, 0.0),
        "max_age_days": max(max_age_days, 0),
    }
    if ledger is not None:
        kwargs["ledger_path"] = ledger
    results = sync_claude_threads_once(**kwargs)
    transferred = sum(1 for result in results if result.status == "transferred")
    conflicts = sum(1 for result in results if result.status == "conflict")
    skipped = sum(1 for result in results if result.status == "skipped")
    click.echo(
        f"Claude thread sync complete: transferred={transferred} conflicts={conflicts} skipped={skipped} total={len(results)}"
    )


@claude_sync.command("run")
@click.option("--interval", default=60.0, show_default=True, type=float)
@click.option("--stability-delay", default=0.2, show_default=True, type=float)
@click.option("--max-age-days", default=15, show_default=True, type=int)
@click.option("--ledger", type=click.Path(path_type=Path), default=None)
@click.option("--verbose", is_flag=True)
def run(
    interval: float,
    stability_delay: float,
    max_age_days: int,
    ledger: Path | None,
    verbose: bool,
) -> None:
    """Run Claude Code thread sync forever on a polling interval."""
    _configure_logging(verbose)
    logger = logging.getLogger(__name__)
    poll_interval = max(interval, 1.0)
    logger.info("claude_thread_sync service_started interval=%s", poll_interval)
    while True:
        started = time.monotonic()
        try:
            kwargs = {
                "stability_delay_seconds": max(stability_delay, 0.0),
                "max_age_days": max(max_age_days, 0),
            }
            if ledger is not None:
                kwargs["ledger_path"] = ledger
            results = sync_claude_threads_once(**kwargs)
            summary = _sync_result_summary(results)
            logger.info(
                "claude_thread_sync sweep_complete total=%s transferred=%s "
                "conflicts=%s errors=%s skipped=%s already_synced=%s "
                "reason_counts=%s direction_counts=%s",
                summary["total"],
                summary["transferred"],
                summary["conflicts"],
                summary["errors"],
                summary["skipped"],
                summary["already_synced"],
                summary["reason_counts"],
                summary["direction_counts"],
            )
        except Exception:
            logger.exception("claude_thread_sync sweep_failed")
        elapsed = time.monotonic() - started
        time.sleep(max(poll_interval - elapsed, 1.0))


@click.group("devices")
def devices() -> None:
    """Synchronize Claude Code thread snapshots across devices."""


@devices.command("init")
def devices_init() -> None:
    """Create or show this machine's thread sync device identity."""
    identity = get_or_create_device_identity()
    click.echo(
        f"Claude thread sync device: id={identity.device_id} name={identity.device_name}"
    )


@devices.command("status")
@click.option(
    "--exchange-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_DEVICE_EXCHANGE_DIR,
    show_default=True,
)
def devices_status(exchange_dir: Path) -> None:
    """Show cross-device Claude thread snapshot sync status."""
    status = claude_thread_snapshot_status(exchange_dir=exchange_dir)
    device = status.get("device") or {}
    click.echo(f"Device: {device.get('device_name') or 'not initialized'}")
    click.echo(f"Device ID: {device.get('device_id') or 'not initialized'}")
    click.echo(f"Exchange folder: {status['exchange_dir']}")
    click.echo(f"Ledger: {status['ledger_path']}")
    click.echo(
        f"Snapshots={status['snapshot_count']} sessions={status['session_count']} conflicts={status['conflict_count']}"
    )


@devices.command("export-once")
@click.option(
    "--exchange-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_DEVICE_EXCHANGE_DIR,
    show_default=True,
)
@click.option("--stability-delay", default=0.2, show_default=True, type=float)
@click.option("--max-age-days", default=15, show_default=True, type=int)
def devices_export_once(
    exchange_dir: Path,
    stability_delay: float,
    max_age_days: int,
) -> None:
    """Export completed local Claude Code thread snapshots."""
    results = export_claude_thread_snapshots(
        exchange_dir=exchange_dir,
        stability_delay_seconds=max(stability_delay, 0.0),
        max_age_days=max(max_age_days, 0),
    )
    summary = _snapshot_result_summary(results)
    click.echo(
        f"Claude device export complete: exported={summary['exported']} skipped={summary['skipped']} already_exported={summary['already_exported']} total={summary['total']}"
    )


@devices.command("import-once")
@click.option(
    "--exchange-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_DEVICE_EXCHANGE_DIR,
    show_default=True,
)
def devices_import_once(exchange_dir: Path) -> None:
    """Import completed Claude Code thread snapshots from other devices."""
    results = import_claude_thread_snapshots(exchange_dir=exchange_dir)
    summary = _snapshot_result_summary(results)
    click.echo(
        f"Claude device import complete: imported={summary['imported']} conflicts={summary['conflicts']} skipped={summary['skipped']} already_imported={summary['already_imported']} total={summary['total']}"
    )


@devices.command("once")
@click.option(
    "--exchange-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_DEVICE_EXCHANGE_DIR,
    show_default=True,
)
@click.option("--stability-delay", default=0.2, show_default=True, type=float)
@click.option("--max-age-days", default=15, show_default=True, type=int)
def devices_once(
    exchange_dir: Path,
    stability_delay: float,
    max_age_days: int,
) -> None:
    """Run one cross-device Claude snapshot export/import pass."""
    result = sync_claude_thread_snapshots_once(
        exchange_dir=exchange_dir,
        stability_delay_seconds=max(stability_delay, 0.0),
        max_age_days=max(max_age_days, 0),
    )
    export_summary = _snapshot_result_summary(result["exports"])
    import_summary = _snapshot_result_summary(result["imports"])
    click.echo(
        f"Claude device sync complete: exported={export_summary['exported']} imported={import_summary['imported']} conflicts={import_summary['conflicts']} total={export_summary['total'] + import_summary['total']}"
    )


@devices.command("run")
@click.option("--interval", default=60.0, show_default=True, type=float)
@click.option(
    "--exchange-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_DEVICE_EXCHANGE_DIR,
    show_default=True,
)
@click.option("--stability-delay", default=0.2, show_default=True, type=float)
@click.option("--max-age-days", default=15, show_default=True, type=int)
@click.option("--verbose", is_flag=True)
def devices_run(
    interval: float,
    exchange_dir: Path,
    stability_delay: float,
    max_age_days: int,
    verbose: bool,
) -> None:
    """Run cross-device Claude snapshot sync forever on a polling interval."""
    _configure_logging(verbose)
    logger = logging.getLogger(__name__)
    poll_interval = max(interval, 1.0)
    logger.info(
        "claude_thread_device_sync service_started interval=%s exchange_dir=%s",
        poll_interval,
        exchange_dir,
    )
    while True:
        started = time.monotonic()
        try:
            result = sync_claude_thread_snapshots_once(
                exchange_dir=exchange_dir,
                stability_delay_seconds=max(stability_delay, 0.0),
                max_age_days=max(max_age_days, 0),
            )
            export_summary = _snapshot_result_summary(result["exports"])
            import_summary = _snapshot_result_summary(result["imports"])
            logger.info(
                "claude_thread_device_sync sweep_complete exported=%s imported=%s "
                "conflicts=%s export_total=%s import_total=%s export_reasons=%s import_reasons=%s",
                export_summary["exported"],
                import_summary["imported"],
                import_summary["conflicts"],
                export_summary["total"],
                import_summary["total"],
                export_summary["reason_counts"],
                import_summary["reason_counts"],
            )
        except Exception:
            logger.exception("claude_thread_device_sync sweep_failed")
        elapsed = time.monotonic() - started
        time.sleep(max(poll_interval - elapsed, 1.0))


claude_sync.add_command(devices)
