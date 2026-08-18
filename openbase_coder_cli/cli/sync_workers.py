"""``openbase-coder sync-workers`` — all periodic sync jobs in one process.

One launchd/systemd service replaces the former quartet of near-identical
polling services (codex-thread-sync, claude-thread-sync,
codex-thread-device-sync, claude-thread-device-sync) and also runs the
code-sync reconcile tick that previously hid inside ``openbase-routines``.

Each job runs on its own thread with its own interval and per-tick error
isolation, so one slow or failing job never delays the others. Jobs that only
apply in certain states gate themselves at runtime (device sync and reconcile
no-op unless code sync is enabled) instead of being installed and removed as
separate services.

Log event names are unchanged from the per-service days
(``codex_thread_sync sweep_complete`` etc.) so existing log greps keep
working. Interval/max-age env overrides keep their historical names
(``CODEX_THREAD_SYNC_INTERVAL`` etc., sourced from ``~/.openbase/.env`` by the
service wrapper).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import click

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_MAX_AGE_DAYS = 15
DEFAULT_STABILITY_DELAY_SECONDS = 0.2
CODE_SYNC_TICK_SECONDS = 60.0
CLOUD_REGISTER_INTERVAL_SECONDS = 3600.0
LIVEKIT_POOL_WATCHDOG_TICK_SECONDS = 30.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


def _code_sync_enabled() -> bool:
    from openbase_coder_cli.sync_config import code_sync_enabled

    try:
        return code_sync_enabled()
    except ValueError:
        return False


@dataclass(frozen=True)
class SyncJob:
    name: str
    interval: float
    tick: Callable[[], None]


def _codex_threads_tick() -> None:
    from openbase_coder_cli.cli.codex_sync import _sync_result_summary
    from openbase_coder_cli.thread_sync.thread_import import sync_codex_threads_once

    results = sync_codex_threads_once(
        stability_delay_seconds=DEFAULT_STABILITY_DELAY_SECONDS,
        max_age_days=max(
            _env_int("CODEX_THREAD_SYNC_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS), 0
        ),
    )
    summary = _sync_result_summary(results)
    logger.info(
        "codex_thread_sync sweep_complete total=%s transferred=%s conflicts=%s "
        "errors=%s skipped=%s already_synced=%s reason_counts=%s direction_counts=%s",
        summary["total"],
        summary["transferred"],
        summary["conflicts"],
        summary["errors"],
        summary["skipped"],
        summary["already_synced"],
        summary["reason_counts"],
        summary["direction_counts"],
    )


def _claude_threads_tick() -> None:
    from openbase_coder_cli.cli.claude_sync import _sync_result_summary
    from openbase_coder_cli.thread_sync.claude_thread_sync import (
        sync_claude_threads_once,
    )

    results = sync_claude_threads_once(
        stability_delay_seconds=DEFAULT_STABILITY_DELAY_SECONDS,
        max_age_days=max(
            _env_int("CLAUDE_THREAD_SYNC_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS), 0
        ),
    )
    summary = _sync_result_summary(results)
    logger.info(
        "claude_thread_sync sweep_complete total=%s transferred=%s conflicts=%s "
        "errors=%s skipped=%s already_synced=%s reason_counts=%s direction_counts=%s",
        summary["total"],
        summary["transferred"],
        summary["conflicts"],
        summary["errors"],
        summary["skipped"],
        summary["already_synced"],
        summary["reason_counts"],
        summary["direction_counts"],
    )


def _codex_devices_tick() -> None:
    # Cross-device snapshots ride the code-sync transport (the exchange folder
    # is a product-state sync folder), so this is a no-op until code sync is
    # enabled — runtime gating replaces the old install-time companion
    # services managed by code_sync.manager.
    if not _code_sync_enabled():
        return
    from openbase_coder_cli.cli.codex_sync import _snapshot_result_summary
    from openbase_coder_cli.thread_sync.thread_exchange import (
        DEFAULT_EXCHANGE_DIR,
        sync_thread_snapshots_once,
    )

    result = sync_thread_snapshots_once(
        exchange_dir=_env_path(
            "CODEX_THREAD_DEVICE_SYNC_EXCHANGE_DIR", DEFAULT_EXCHANGE_DIR
        ),
        stability_delay_seconds=DEFAULT_STABILITY_DELAY_SECONDS,
        max_age_days=max(
            _env_int("CODEX_THREAD_DEVICE_SYNC_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS), 0
        ),
    )
    export_summary = _snapshot_result_summary(result["exports"])
    import_summary = _snapshot_result_summary(result["imports"])
    logger.info(
        "codex_thread_device_sync sweep_complete exported=%s imported=%s "
        "conflicts=%s export_total=%s import_total=%s export_reasons=%s "
        "import_reasons=%s",
        export_summary["exported"],
        import_summary["imported"],
        import_summary["conflicts"],
        export_summary["total"],
        import_summary["total"],
        export_summary["reason_counts"],
        import_summary["reason_counts"],
    )


def _claude_devices_tick() -> None:
    if not _code_sync_enabled():
        return
    from openbase_coder_cli.cli.claude_sync import _snapshot_result_summary
    from openbase_coder_cli.thread_sync.claude_thread_sync import (
        DEFAULT_DEVICE_EXCHANGE_DIR,
        sync_claude_thread_snapshots_once,
    )

    result = sync_claude_thread_snapshots_once(
        exchange_dir=_env_path(
            "CLAUDE_THREAD_DEVICE_SYNC_EXCHANGE_DIR", DEFAULT_DEVICE_EXCHANGE_DIR
        ),
        stability_delay_seconds=DEFAULT_STABILITY_DELAY_SECONDS,
        max_age_days=max(
            _env_int("CLAUDE_THREAD_DEVICE_SYNC_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS), 0
        ),
    )
    export_summary = _snapshot_result_summary(result["exports"])
    import_summary = _snapshot_result_summary(result["imports"])
    logger.info(
        "claude_thread_device_sync sweep_complete exported=%s imported=%s "
        "conflicts=%s export_total=%s import_total=%s export_reasons=%s "
        "import_reasons=%s",
        export_summary["exported"],
        import_summary["imported"],
        import_summary["conflicts"],
        export_summary["total"],
        import_summary["total"],
        export_summary["reason_counts"],
        import_summary["reason_counts"],
    )


def _cloud_registration_tick() -> None:
    from openbase_coder_cli.config.token_manager import (
        DEFAULT_WEB_BACKEND_URL,
        TokenManager,
    )
    from openbase_coder_cli.services.cloud_registration import register_and_report

    # Login-time registration is one-shot and fails silently on network or
    # backend blips, leaving the machine invisible to the account until the
    # next login. This periodic re-report self-heals; the first tick runs at
    # service start, so a restart also re-registers promptly.
    if not TokenManager(DEFAULT_WEB_BACKEND_URL).has_refresh_token:
        logger.debug("cloud_registration skipped no_login")
        return
    result = register_and_report()
    if result.ok:
        logger.info("cloud_registration report_complete")
    elif not result.supported:
        logger.debug("cloud_registration endpoint_unsupported")
    else:
        logger.warning(
            "cloud_registration report_failed error=%s status=%s",
            result.error,
            result.status_code,
        )


def _livekit_pool_watchdog_tick() -> None:
    # Detect the stale pre-warmed job pool (calls that log
    # ``wait_pc_connection timed out`` and never reach a live agent) and
    # self-heal by bouncing livekit-agent — escalating to server+agent on a
    # recurrence — plus proactively recycle the idle agent so the pool never
    # goes stale. Guarded by an active-call check and a rolling rate limit;
    # all the logic lives in the dedicated module to keep this file thin.
    from openbase_coder_cli.services.livekit_pool_watchdog import run_tick

    run_tick()


def _code_sync_reconcile_tick() -> None:
    from openbase_coder_cli.code_sync.reconciler import (
        reconcile_counts,
        run_tick_if_enabled,
    )

    summary = run_tick_if_enabled()
    if summary is None:
        return
    counts = reconcile_counts(summary)
    logger.info(
        "code_sync tick_complete repos=%s up_to_date=%s fast_forwarded=%s "
        "awaiting_files=%s remote_behind=%s diverged=%s skipped=%s "
        "fetch_failed=%s converged=%s published=%s conflicts=%s errors=%s "
        "lease=%s",
        counts["repo_count"],
        counts["up_to_date"],
        counts["fast_forwarded"],
        counts["awaiting_files"],
        counts["remote_behind"],
        counts["diverged"],
        counts["skipped"],
        counts["fetch_failed"],
        counts["converged"],
        counts["published"],
        summary.get("conflicts_count"),
        counts["errors"],
        summary.get("lease", {}).get("action"),
    )
    if summary.get("errors"):
        logger.warning("code_sync tick_errors %s", summary["errors"])


def build_jobs() -> list[SyncJob]:
    """The full job set; gating happens inside each tick, not here."""
    return [
        SyncJob(
            name="codex_thread_sync",
            interval=_env_float("CODEX_THREAD_SYNC_INTERVAL", DEFAULT_INTERVAL_SECONDS),
            tick=_codex_threads_tick,
        ),
        SyncJob(
            name="claude_thread_sync",
            interval=_env_float(
                "CLAUDE_THREAD_SYNC_INTERVAL", DEFAULT_INTERVAL_SECONDS
            ),
            tick=_claude_threads_tick,
        ),
        SyncJob(
            name="codex_thread_device_sync",
            interval=_env_float(
                "CODEX_THREAD_DEVICE_SYNC_INTERVAL", DEFAULT_INTERVAL_SECONDS
            ),
            tick=_codex_devices_tick,
        ),
        SyncJob(
            name="claude_thread_device_sync",
            interval=_env_float(
                "CLAUDE_THREAD_DEVICE_SYNC_INTERVAL", DEFAULT_INTERVAL_SECONDS
            ),
            tick=_claude_devices_tick,
        ),
        SyncJob(
            name="code_sync_reconcile",
            interval=_env_float("CODE_SYNC_TICK_SECONDS", CODE_SYNC_TICK_SECONDS),
            tick=_code_sync_reconcile_tick,
        ),
        SyncJob(
            name="cloud_registration",
            interval=_env_float(
                "OPENBASE_CLOUD_REGISTER_INTERVAL", CLOUD_REGISTER_INTERVAL_SECONDS
            ),
            tick=_cloud_registration_tick,
        ),
        SyncJob(
            name="livekit_pool_watchdog",
            interval=_env_float(
                "LIVEKIT_POOL_WATCHDOG_TICK_SECONDS",
                LIVEKIT_POOL_WATCHDOG_TICK_SECONDS,
            ),
            tick=_livekit_pool_watchdog_tick,
        ),
    ]


def _job_loop(job: SyncJob, stop: threading.Event) -> None:
    poll_interval = max(job.interval, 1.0)
    while not stop.is_set():
        started = time.monotonic()
        try:
            job.tick()
        except Exception:
            logger.exception("%s sweep_failed", job.name)
        elapsed = time.monotonic() - started
        stop.wait(max(poll_interval - elapsed, 1.0))


def run_workers(stop: threading.Event | None = None) -> list[threading.Thread]:
    """Start one thread per job; returns the threads (daemon)."""
    stop = stop or threading.Event()
    jobs = build_jobs()
    logger.info(
        "sync_workers service_started jobs=%s",
        [(job.name, job.interval) for job in jobs],
    )
    threads = []
    for job in jobs:
        thread = threading.Thread(
            target=_job_loop, args=(job, stop), name=job.name, daemon=True
        )
        thread.start()
        threads.append(thread)
    return threads


@click.group("sync-workers")
def sync_workers() -> None:
    """Combined thread/device/code-sync workers (one service process)."""


@sync_workers.command("run")
@click.option("--verbose", is_flag=True)
def run(verbose: bool) -> None:
    """Run every sync job forever, each on its own thread."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(asctime)s %(name)s %(message)s",
    )
    stop = threading.Event()
    run_workers(stop)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
