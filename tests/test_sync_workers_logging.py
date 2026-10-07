from __future__ import annotations

import importlib

from openbase_coder_cli import sync_daemon
from openbase_coder_cli.thread_sync import claude_thread_sync, thread_exchange

# The cli package re-exports a click group named sync_workers; import the
# module itself to reach the tick functions.
sync_workers = importlib.import_module("openbase_coder_cli.cli.sync_workers")


def _configure_daemon(roots: list[str]) -> None:
    sync_daemon.write_config(
        sync_daemon.SyncDaemonConfig(
            device_id="laptop",
            sync_group="default",
            role="edge",
            pair_secret="s",
            roots=[{"id": sync_daemon.root_id_for_path(r), "path": r} for r in roots],
            peer_hot="hub:22100",
            peer_bulk="hub:22101",
        )
    )


def _record_calls(monkeypatch) -> list[str]:
    calls: list[str] = []
    empty = {"exports": [], "imports": []}

    def codex(**kwargs):
        calls.append(f"codex:{kwargs['exchange_dir']}")
        return empty

    def claude(**kwargs):
        calls.append(f"claude:{kwargs['exchange_dir']}")
        return empty

    monkeypatch.setattr(thread_exchange, "sync_thread_snapshots_once", codex)
    monkeypatch.setattr(
        claude_thread_sync, "sync_claude_thread_snapshots_once", claude
    )
    return calls


def test_device_ticks_skip_when_openbase_sync_is_not_configured(
    monkeypatch, tmp_path
) -> None:
    calls = _record_calls(monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_DEVICE_SYNC_EXCHANGE_DIR", str(tmp_path / "x"))
    monkeypatch.setenv("CLAUDE_THREAD_DEVICE_SYNC_EXCHANGE_DIR", str(tmp_path / "x"))

    sync_workers._codex_devices_tick()
    sync_workers._claude_devices_tick()

    assert calls == []


def test_device_ticks_skip_when_exchange_is_outside_every_root(
    monkeypatch, tmp_path
) -> None:
    _configure_daemon([str(tmp_path / "Projects")])
    calls = _record_calls(monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_DEVICE_SYNC_EXCHANGE_DIR", str(tmp_path / "x"))

    sync_workers._codex_devices_tick()

    assert calls == []


def test_device_ticks_run_when_a_root_mirrors_the_exchange(
    monkeypatch, tmp_path
) -> None:
    exchange = tmp_path / "state" / "thread-sync"
    _configure_daemon([str(tmp_path / "state")])
    calls = _record_calls(monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_DEVICE_SYNC_EXCHANGE_DIR", str(exchange))
    monkeypatch.setenv("CLAUDE_THREAD_DEVICE_SYNC_EXCHANGE_DIR", str(exchange))

    sync_workers._codex_devices_tick()
    sync_workers._claude_devices_tick()

    assert calls == [f"codex:{exchange}", f"claude:{exchange}"]
