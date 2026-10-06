"""Discard dead pre-warmed job processes from the livekit-agents ProcPool.

After macOS sleep, the ping watchdog kills every idle pre-warmed job process
("process is unresponsive, killing process"), but upstream ``ProcPool`` leaves
the dead executors in its warmed queue and counts them toward its idle
target. Every dispatched job then fails instantly with ``BrokenPipeError``
("failed to launch job on process after 3 attempts") — calls sit on "waiting
for agent" — and no replacements spawn until enough failed calls drain the
corpses. Upstream report: https://github.com/livekit/agents/issues/3841.

Two patch layers, both transparent for healthy processes:

- ``_acquire_proc`` skips dead executors instead of handing them to
  ``launch_job``, so the first call after wake self-heals instead of failing.
- A ``process_closed`` listener purges dead executors from the warmed queue
  so the pool's idle accounting stays honest and it re-warms right away.

The patch only applies to exact livekit-agents versions we have verified;
bumping the pin without revisiting this module logs an error and leaves the
stock (broken) behavior in place rather than patching blind.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Exact livekit-agents versions whose ProcPool internals this patch matches.
# 1.8.4 re-verified 2026-10-06: ``_acquire_proc(job_id)``, ``start``,
# ``_warmed_proc_queue``, ``_executors``, ``_close_tasks`` and the
# ``process_closed`` event are unchanged, and ``launch_job`` still hands a
# killed executor to the job (issue 3841 is open), so the patch is still needed.
PATCHED_LIVEKIT_AGENTS_VERSIONS = frozenset({"1.5.17", "1.8.4"})

# Upper bound on corpses discarded per acquire; past it we fall back to stock
# behavior so a pathological spawn-then-die loop cannot hang job dispatch.
MAX_DEAD_PROC_DISCARDS = 64

_installed = False


def _executor_is_dead(pool: Any, proc: Any) -> bool:
    if proc not in pool._executors:
        return True
    if getattr(proc, "killed", False):
        return True
    if getattr(proc, "exitcode", None) is not None:
        return True
    return False


def _schedule_close(pool: Any, proc: Any) -> None:
    # Mirror upstream launch_job's failure path: close in the background and
    # track the task on the pool so aclose() can await it.
    close_task = asyncio.create_task(proc.aclose())
    pool._close_tasks.add(close_task)
    close_task.add_done_callback(pool._close_tasks.discard)


def _purge_dead_procs(pool: Any) -> None:
    """Rebuild the warmed queue without dead executors.

    No awaits between drain and refill, so waiters blocked on the queue see
    a single atomic operation on the event loop.
    """
    try:
        survivors = []
        dead = []
        while True:
            try:
                proc = pool._warmed_proc_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if _executor_is_dead(pool, proc):
                dead.append(proc)
            else:
                survivors.append(proc)
        for proc in survivors:
            pool._warmed_proc_queue.put_nowait(proc)
        for proc in dead:
            logger.warning(
                "purging dead pre-warmed job process from pool",
                extra={"pid": getattr(proc, "pid", None)},
            )
            _schedule_close(pool, proc)
    except Exception:
        logger.exception("failed to purge dead pre-warmed job processes")


def install_proc_pool_liveness_patch() -> bool:
    """Patch ProcPool so dead pre-warmed processes are never handed to jobs.

    Returns True when the patch is active (freshly applied or already
    installed), False when it declined to apply.
    """
    global _installed
    if _installed:
        return True

    import livekit.agents
    from livekit.agents.ipc.proc_pool import ProcPool

    version = livekit.agents.__version__
    if version not in PATCHED_LIVEKIT_AGENTS_VERSIONS:
        logger.error(
            "livekit-agents %s is not a version verified for the ProcPool "
            "liveness patch (%s); leaving stock behavior in place — dead "
            "pre-warmed processes after Mac sleep will fail calls until "
            "openbase_coder_cli.livekit_agent.proc_pool_patch is revisited "
            "(see https://github.com/livekit/agents/issues/3841)",
            version,
            ", ".join(sorted(PATCHED_LIVEKIT_AGENTS_VERSIONS)),
        )
        return False

    original_acquire_proc = ProcPool._acquire_proc
    original_start = ProcPool.start

    @functools.wraps(original_acquire_proc)
    async def _acquire_proc_skipping_dead(self: Any, job_id: str) -> Any:
        for _ in range(MAX_DEAD_PROC_DISCARDS):
            proc = await original_acquire_proc(self, job_id)
            if not _executor_is_dead(self, proc):
                return proc
            logger.warning(
                "discarding dead pre-warmed job process before launch",
                extra={"job_id": job_id, "pid": getattr(proc, "pid", None)},
            )
            _schedule_close(self, proc)
        logger.error(
            "discarded %d dead pre-warmed job processes for a single job; "
            "falling back to stock acquire",
            MAX_DEAD_PROC_DISCARDS,
            extra={"job_id": job_id},
        )
        return await original_acquire_proc(self, job_id)

    @functools.wraps(original_start)
    async def _start_with_purge_listener(self: Any) -> None:
        if not getattr(self, "_openbase_purge_listener_installed", False):
            self._openbase_purge_listener_installed = True
            self.on("process_closed", lambda _proc: _purge_dead_procs(self))
        await original_start(self)

    ProcPool._acquire_proc = _acquire_proc_skipping_dead
    ProcPool.start = _start_with_purge_listener
    _installed = True
    logger.info("installed ProcPool liveness patch for livekit-agents %s", version)
    return True
