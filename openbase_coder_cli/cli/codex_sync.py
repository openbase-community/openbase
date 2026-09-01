from __future__ import annotations

import logging
import time
from collections import Counter
from pathlib import Path

import click

from openbase_coder_cli.thread_sync.thread_exchange import (
    DEFAULT_EXCHANGE_DIR,
    DEFAULT_LEDGER_PATH,
    ThreadSnapshotResult,
    export_thread_snapshots,
    get_or_create_device_identity,
    import_thread_snapshots,
    sync_thread_snapshots_once,
    thread_snapshot_status,
)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(asctime)s %(name)s %(message)s",
    )


def _format_counts(counts: Counter[str]) -> str:
    if not counts:
        return "none"
    return ",".join(f"{key}:{counts[key]}" for key in sorted(counts))


def _snapshot_result_summary(
    results: list[ThreadSnapshotResult],
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


@click.group("codex-sync")
def codex_sync() -> None:
    """Synchronize Codex thread snapshots across devices."""


@click.group("devices")
def devices() -> None:
    """Synchronize Codex thread snapshots across devices."""


@devices.command("init")
def devices_init() -> None:
    """Create or show this machine's thread sync device identity."""
    identity = get_or_create_device_identity()
    click.echo(
        f"Codex thread sync device: id={identity.device_id} name={identity.device_name}"
    )


@devices.command("status")
@click.option(
    "--exchange-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_EXCHANGE_DIR,
    show_default=True,
)
def devices_status(exchange_dir: Path) -> None:
    """Show cross-device thread snapshot sync status."""
    status = thread_snapshot_status(exchange_dir=exchange_dir)
    device = status.get("device") or {}
    click.echo(f"Device: {device.get('device_name') or 'not initialized'}")
    click.echo(f"Device ID: {device.get('device_id') or 'not initialized'}")
    click.echo(f"Exchange folder: {status['exchange_dir']}")
    click.echo(f"Ledger: {status['ledger_path']}")
    click.echo(
        f"Snapshots={status['snapshot_count']} threads={status['thread_count']} conflicts={status['conflict_count']}"
    )


@devices.command("export-once")
@click.option(
    "--exchange-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_EXCHANGE_DIR,
    show_default=True,
)
@click.option("--stability-delay", default=0.2, show_default=True, type=float)
@click.option("--max-age-days", default=15, show_default=True, type=int)
def devices_export_once(
    exchange_dir: Path,
    stability_delay: float,
    max_age_days: int,
) -> None:
    """Export completed local Codex thread snapshots."""
    results = export_thread_snapshots(
        exchange_dir=exchange_dir,
        stability_delay_seconds=max(stability_delay, 0.0),
        max_age_days=max(max_age_days, 0),
    )
    summary = _snapshot_result_summary(results)
    click.echo(
        f"Codex device export complete: exported={summary['exported']} skipped={summary['skipped']} already_exported={summary['already_exported']} total={summary['total']}"
    )


@devices.command("import-once")
@click.option(
    "--exchange-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_EXCHANGE_DIR,
    show_default=True,
)
def devices_import_once(exchange_dir: Path) -> None:
    """Import completed Codex thread snapshots from other devices."""
    results = import_thread_snapshots(exchange_dir=exchange_dir)
    summary = _snapshot_result_summary(results)
    click.echo(
        f"Codex device import complete: imported={summary['imported']} conflicts={summary['conflicts']} skipped={summary['skipped']} already_imported={summary['already_imported']} total={summary['total']}"
    )


@devices.command("once")
@click.option(
    "--exchange-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_EXCHANGE_DIR,
    show_default=True,
)
@click.option("--stability-delay", default=0.2, show_default=True, type=float)
@click.option("--max-age-days", default=15, show_default=True, type=int)
def devices_once(
    exchange_dir: Path,
    stability_delay: float,
    max_age_days: int,
) -> None:
    """Run one cross-device snapshot export/import pass."""
    result = sync_thread_snapshots_once(
        exchange_dir=exchange_dir,
        stability_delay_seconds=max(stability_delay, 0.0),
        max_age_days=max(max_age_days, 0),
    )
    export_summary = _snapshot_result_summary(result["exports"])
    import_summary = _snapshot_result_summary(result["imports"])
    click.echo(
        f"Codex device sync complete: exported={export_summary['exported']} imported={import_summary['imported']} conflicts={import_summary['conflicts']} total={export_summary['total'] + import_summary['total']}"
    )


@devices.command("run")
@click.option("--interval", default=60.0, show_default=True, type=float)
@click.option(
    "--exchange-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_EXCHANGE_DIR,
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
    """Run cross-device snapshot sync forever on a polling interval."""
    _configure_logging(verbose)
    logger = logging.getLogger(__name__)
    poll_interval = max(interval, 1.0)
    logger.info(
        "codex_thread_device_sync service_started interval=%s exchange_dir=%s ledger=%s",
        poll_interval,
        exchange_dir,
        DEFAULT_LEDGER_PATH,
    )
    while True:
        started = time.monotonic()
        try:
            result = sync_thread_snapshots_once(
                exchange_dir=exchange_dir,
                stability_delay_seconds=max(stability_delay, 0.0),
                max_age_days=max(max_age_days, 0),
            )
            export_summary = _snapshot_result_summary(result["exports"])
            import_summary = _snapshot_result_summary(result["imports"])
            logger.info(
                "codex_thread_device_sync sweep_complete exported=%s imported=%s "
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
            logger.exception("codex_thread_device_sync sweep_failed")
        elapsed = time.monotonic() - started
        time.sleep(max(poll_interval - elapsed, 1.0))


codex_sync.add_command(devices)
