import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openbase_coder_cli import workspace_memory as memory
from openbase_coder_cli.container_turns import container_client_type


@pytest.fixture
def container(monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_SERVICE_SUPERVISOR", "external")
    monkeypatch.setattr(
        memory,
        "memory_snapshot",
        lambda: memory.Memory(4096 * memory.MIB, 3000 * memory.MIB),
    )


def test_cgroup_accounting(monkeypatch, tmp_path):
    monkeypatch.setattr(
        memory.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=8192, available=6000),
    )
    (tmp_path / "memory.max").write_text("4096")
    (tmp_path / "memory.current").write_text("3500")
    (tmp_path / "memory.stat").write_text("inactive_file 100\n")
    assert memory.memory_snapshot(tmp_path) == memory.Memory(4096, 696)
    (tmp_path / "memory.max").write_text("max")
    assert memory.memory_snapshot(tmp_path) == memory.Memory(8192, 6000)


def test_slots_release_and_busy(container, tmp_path):
    leases = [memory.acquire_turn_slot(tmp_path) for _ in range(2)]
    try:
        with pytest.raises(memory.WorkspaceBusy, match="Workspace busy"):
            memory.acquire_turn_slot(tmp_path)
        leases.pop().close()
        leases.append(memory.acquire_turn_slot(tmp_path))
    finally:
        for lease in leases:
            lease.close()


def test_pressure_rejects_before_spawning(container, monkeypatch, tmp_path):
    monkeypatch.setattr(
        memory,
        "memory_snapshot",
        lambda: memory.Memory(4096 * memory.MIB, 1000 * memory.MIB),
    )
    with pytest.raises(memory.WorkspaceBusy):
        memory.acquire_turn_slot(tmp_path)


def test_desktop_unchanged(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENBASE_CODER_SERVICE_SUPERVISOR", raising=False)
    assert memory.acquire_turn_slot(tmp_path) is None
    assert container_client_type(object) is object


def test_watchdog_logs_only_pressure_and_no_arguments(container, monkeypatch, caplog):
    monkeypatch.setattr(
        memory.psutil,
        "process_iter",
        lambda: [
            SimpleNamespace(
                pid=123,
                ppid=lambda: 1,
                name=lambda: "python",
                memory_info=lambda: SimpleNamespace(rss=1200 * memory.MIB),
            )
        ],
    )
    with caplog.at_level(logging.WARNING):
        memory.log_memory_pressure()
        assert not caplog.records
        monkeypatch.setattr(
            memory,
            "memory_snapshot",
            lambda: memory.Memory(4096 * memory.MIB, 500 * memory.MIB),
        )
        memory.log_memory_pressure()
    assert "workspace_memory_pressure" in caplog.text
    assert "1200" in caplog.text
    assert "123" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, RuntimeError("failed"), asyncio.CancelledError()]
)
async def test_turn_releases_memory_and_idle_cli(
    container, monkeypatch, tmp_path, failure
):
    import openbase_coder_cli.container_turns as turns

    monkeypatch.setattr(
        turns, "acquire_turn_slot", lambda: memory.acquire_turn_slot(tmp_path)
    )

    class Client:
        _disconnect_sdk_client = AsyncMock()

        @asynccontextmanager
        async def _cross_process_session_lock(self, session_id):
            yield

        async def _run_turn(self, session_id):
            async with self._cross_process_session_lock(session_id):
                self._reserve_memory(session_id)
                if failure:
                    raise failure

    client = container_client_type(Client)()
    if failure:
        with pytest.raises(type(failure)):
            await client._run_turn("a")
    else:
        await client._run_turn("a")
    assert not client._memory_leases()
    client._disconnect_sdk_client.assert_awaited_with("a")
    leases = [memory.acquire_turn_slot(tmp_path) for _ in range(2)]
    for lease in leases:
        lease.close()


def test_locks_are_shared_with_other_processes(container, tmp_path):
    import subprocess
    import sys

    lease = memory.acquire_turn_slot(tmp_path)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)",
                str(tmp_path / "turn-0.lock"),
            ],
            capture_output=True,
        )
        assert result.returncode != 0
        assert b"BlockingIOError" in result.stderr
    finally:
        lease.close()


@pytest.mark.asyncio
async def test_container_warmup_does_not_spawn_or_claim_slot(container):
    class Client:
        def _resolve_session(self, query):
            return query

        def _warm_result(self, session, **kwargs):
            return kwargs

    client = container_client_type(Client)()
    assert await client.warm_session_by_label("a") == {
        "warmed": False,
        "reason": "container_on_demand",
    }
    assert not client._memory_leases()


def test_launch_failure_releases_claim(container, monkeypatch, tmp_path):
    import openbase_coder_cli.container_turns as turns

    monkeypatch.setattr(
        turns, "acquire_turn_slot", lambda: memory.acquire_turn_slot(tmp_path)
    )

    class Client:
        def _launch_turn(self, session):
            raise ValueError("invalid input")

    client = container_client_type(Client)()
    with pytest.raises(ValueError, match="invalid input"):
        client._launch_turn(SimpleNamespace(id="a"))
    assert not client._memory_leases()
