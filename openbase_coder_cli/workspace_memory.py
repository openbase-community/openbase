"""Container memory accounting and cross-process admission for expensive work."""

from __future__ import annotations

import heapq
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import psutil

MIB = 1024 * 1024
TURN_BUDGET = 768 * MIB
VOICE_RESERVE = 2048 * MIB
logger = logging.getLogger(__name__)


def is_container() -> bool:
    return os.environ.get("OPENBASE_CODER_SERVICE_SUPERVISOR") == "external"


@dataclass(frozen=True)
class Memory:
    total: int
    available: int

    @property
    def used(self) -> int:
        return self.total - self.available


def memory_snapshot(root: Path = Path("/sys/fs/cgroup")) -> Memory:
    """Honor Linux cgroup limits; Maritime VMs fall back to guest MemAvailable."""
    host = psutil.virtual_memory()
    total, available = host.total, host.available
    for limit_path, usage_path, stat_path, cache_key in (
        (
            root / "memory.max",
            root / "memory.current",
            root / "memory.stat",
            "inactive_file",
        ),
        (
            root / "memory/memory.limit_in_bytes",
            root / "memory/memory.usage_in_bytes",
            root / "memory/memory.stat",
            "total_inactive_file",
        ),
    ):
        try:
            limit = int(limit_path.read_text().strip())
            usage = int(usage_path.read_text().strip())
            stats = dict(line.split() for line in stat_path.read_text().splitlines())
        except (OSError, ValueError):
            continue  # No finite cgroup limit: use guest memory accounting.
        if 0 < limit < total:
            total = limit
            available = min(
                available, max(0, limit - usage + int(stats.get(cache_key, "0")))
            )
    return Memory(total, min(total, available))


class WorkspaceBusy(RuntimeError):
    def __init__(self):
        super().__init__(
            "Workspace busy: memory is reserved for active work and your call. Wait for an agent or build to finish, then retry."
        )


def acquire_turn_slot(directory: Path | None = None):
    """Hold an OS lock through the turn; process death releases it automatically."""
    if not is_container():
        return None
    import fcntl

    if directory is None:
        from openbase_coder_cli.cli.utils import get_data_dir

        directory = get_data_dir() / "run" / "memory-slots"
    directory.mkdir(parents=True, exist_ok=True)
    memory = memory_snapshot()
    if memory.available < TURN_BUDGET + 256 * MIB:
        raise WorkspaceBusy()
    slots = max(1, (memory.total - VOICE_RESERVE) // TURN_BUDGET)
    for index in range(slots):
        handle = (directory / f"turn-{index}.lock").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            continue
        # Each occupied slot reserves one budget even before its process grows.
        return handle
    raise WorkspaceBusy()


def log_memory_pressure() -> None:
    if not is_container():
        return
    memory = memory_snapshot()
    if memory.used < memory.total * 0.8:
        return

    def processes():
        for proc in psutil.process_iter():
            try:
                yield (proc.memory_info().rss, proc.pid, proc.ppid(), proc.name()[:64])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue  # Process exited between enumeration and sampling.

    top = heapq.nlargest(10, processes())
    logger.warning(
        "workspace_memory_pressure used_mib=%d total_mib=%d top_rss=%s",
        memory.used // MIB,
        memory.total // MIB,
        [
            {"pid": pid, "ppid": ppid, "name": name, "rss_mib": rss // MIB}
            for rss, pid, ppid, name in top
        ],
    )
