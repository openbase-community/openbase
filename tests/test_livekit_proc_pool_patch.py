"""Tests for the ProcPool liveness patch (dead pre-warmed process pool).

Covers the failure mode where macOS sleep kills every idle pre-warmed job
process but upstream ProcPool keeps handing the corpses to new jobs
(https://github.com/livekit/agents/issues/3841).
"""

import asyncio
import multiprocessing
from types import SimpleNamespace

from livekit.agents.ipc.proc_pool import ProcPool
from livekit.agents.job import JobExecutorType

from openbase_coder_cli.livekit_agent import proc_pool_patch
from openbase_coder_cli.livekit_agent.proc_pool_patch import (
    install_proc_pool_liveness_patch,
)


class _FakeExecutor:
    def __init__(self, *, killed: bool = False, exitcode: int | None = None):
        self.killed = killed
        self.exitcode = exitcode
        self.pid = 12345
        self.launched: list = []
        self.closed = False

    async def launch_job(self, info) -> None:
        if self.killed or self.exitcode is not None:
            raise BrokenPipeError("dead process")
        self.launched.append(info)

    async def aclose(self) -> None:
        self.closed = True


def _make_pool() -> ProcPool:
    async def _entrypoint(ctx) -> None:  # pragma: no cover - never launched
        pass

    return ProcPool(
        initialize_process_fnc=lambda proc: None,
        job_entrypoint_fnc=_entrypoint,
        session_end_fnc=None,
        simulation_end_fnc=None,
        num_idle_processes=0,
        initialize_timeout=1.0,
        close_timeout=1.0,
        session_end_timeout=1.0,
        inference_executor=None,
        job_executor_type=JobExecutorType.PROCESS,
        mp_ctx=multiprocessing.get_context("spawn"),
        memory_warn_mb=0,
        memory_limit_mb=0,
        http_proxy=None,
        loop=asyncio.get_event_loop(),
    )


def _job_info(job_id: str = "job-1") -> SimpleNamespace:
    return SimpleNamespace(job=SimpleNamespace(id=job_id))


async def _drain_close_tasks(pool: ProcPool) -> None:
    while pool._close_tasks:
        await asyncio.gather(*list(pool._close_tasks))


async def test_install_declines_on_unexpected_version(monkeypatch):
    import livekit.agents

    monkeypatch.setattr(proc_pool_patch, "_installed", False)
    monkeypatch.setattr(livekit.agents, "__version__", "9.9.9")
    acquire_before = ProcPool._acquire_proc
    start_before = ProcPool.start

    assert install_proc_pool_liveness_patch() is False
    assert ProcPool._acquire_proc is acquire_before
    assert ProcPool.start is start_before


async def test_launch_job_skips_dead_procs():
    assert install_proc_pool_liveness_patch() is True

    pool = _make_pool()
    dead_killed = _FakeExecutor(killed=True)
    dead_removed = _FakeExecutor()
    dead_exited = _FakeExecutor(exitcode=0)
    live = _FakeExecutor()

    # dead_removed simulates a corpse the monitor already reaped from
    # _executors; the others died but have not been reaped yet.
    pool._executors.extend([dead_killed, dead_exited, live])
    for proc in (dead_killed, dead_removed, dead_exited, live):
        pool._warmed_proc_queue.put_nowait(proc)

    info = _job_info()
    await pool.launch_job(info)

    assert live.launched == [info]
    await _drain_close_tasks(pool)
    assert dead_killed.closed
    assert dead_removed.closed
    assert dead_exited.closed
    assert not live.closed


async def test_launch_job_retries_past_discard_cap(monkeypatch):
    assert install_proc_pool_liveness_patch() is True
    # Force the fallback path: one discard, then stock acquire returns the
    # next corpse, whose launch failure triggers upstream's retry.
    monkeypatch.setattr(proc_pool_patch, "MAX_DEAD_PROC_DISCARDS", 1)

    pool = _make_pool()
    dead_one = _FakeExecutor(killed=True)
    dead_two = _FakeExecutor(killed=True)
    live = _FakeExecutor()
    pool._executors.extend([dead_one, dead_two, live])
    for proc in (dead_one, dead_two, live):
        pool._warmed_proc_queue.put_nowait(proc)

    info = _job_info()
    await pool.launch_job(info)

    assert live.launched == [info]
    await _drain_close_tasks(pool)
    assert dead_one.closed
    assert dead_two.closed


async def test_purge_on_process_closed():
    assert install_proc_pool_liveness_patch() is True

    pool = _make_pool()
    dead = _FakeExecutor(killed=True)
    live = _FakeExecutor()
    pool._executors.extend([dead, live])
    pool._warmed_proc_queue.put_nowait(dead)
    pool._warmed_proc_queue.put_nowait(live)

    await pool.start()
    try:
        pool.emit("process_closed", dead)

        assert pool._warmed_proc_queue.qsize() == 1
        assert pool._warmed_proc_queue.get_nowait() is live
        await _drain_close_tasks(pool)
        assert dead.closed
        assert not live.closed
        pool._warmed_proc_queue.put_nowait(live)
    finally:
        await pool.aclose()
