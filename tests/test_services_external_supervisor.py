"""Service restarts under an external supervisor (the container entrypoint).

Under ``OPENBASE_CODER_SERVICE_SUPERVISOR=external`` there is no launchd or
systemd job: a restart terminates the pidfile's process and the supervisor
relaunches the wrapper. Falling through to ``systemctl`` (absent in the
container) killed the process and then raised (2026-10-07 Maritime wake).
"""

from __future__ import annotations

import pytest

from openbase_coder_cli.services import launchd
from openbase_coder_cli.services.registry import find_service


@pytest.fixture
def external(monkeypatch, tmp_path):
    svc = find_service("livekit-agent")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    wrapper = tmp_path / "livekit-agent.sh"
    wrapper.write_text("#!/bin/bash\n", encoding="utf-8")
    pidfile = run_dir / "livekit-agent.pid"
    pidfile.write_text("4242", encoding="utf-8")
    terminated: list[tuple[int, bool]] = []
    cleaned: list[frozenset[int]] = []

    def terminate(pid: int, *, force: bool = False) -> None:
        terminated.append((pid, force))
        pidfile.unlink(missing_ok=True)  # the supervisor drops the pidfile

    def never(*_args, **_kwargs):
        raise AssertionError("platform service manager must not be used")

    monkeypatch.setattr(launchd, "_external_supervisor", lambda: True)
    monkeypatch.setattr(launchd, "EXTERNAL_SUPERVISOR_RUN_DIR", run_dir)
    monkeypatch.setattr(launchd, "_wrapper_path", lambda _svc: wrapper)
    monkeypatch.setattr(launchd, "_is_macos", lambda: False)
    monkeypatch.setattr(launchd.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(launchd.process_utils, "terminate", terminate)
    monkeypatch.setattr(launchd.process_utils, "process_tree_pids", lambda pid: {pid})
    monkeypatch.setattr(
        launchd,
        "_cleanup_lingering_processes",
        lambda _svc, keep=frozenset(): cleaned.append(keep),
    )
    monkeypatch.setattr(launchd, "_launchctl", never)
    monkeypatch.setattr("openbase_coder_cli.services.systemd.systemd_bootstrap", never)
    monkeypatch.setattr("openbase_coder_cli.services.systemd.systemd_kill", never)
    monkeypatch.setattr("openbase_coder_cli.services.systemd.systemd_bootout", never)
    return svc, pidfile, terminated, cleaned


def test_restart_terminates_the_supervised_process_and_clears_orphans(external):
    svc, pidfile, terminated, cleaned = external

    assert launchd.launchctl_restart(svc) is True
    assert terminated == [(4242, False)]
    assert cleaned == [frozenset()]
    assert not pidfile.exists()


def test_bootstrap_and_bootout_and_kill_never_reach_a_platform_manager(external):
    svc, pidfile, terminated, _cleaned = external

    launchd.launchctl_bootstrap(svc)
    pidfile.write_text("4243", encoding="utf-8")
    assert launchd.launchctl_bootout(svc) is True
    pidfile.write_text("4244", encoding="utf-8")
    assert launchd.launchctl_kill(svc) is True
    assert [pid for pid, _force in terminated] == [4242, 4243, 4244]


def test_restart_without_a_running_process_is_a_no_op(external):
    svc, pidfile, terminated, cleaned = external
    pidfile.unlink()

    assert launchd.launchctl_restart(svc) is True
    assert terminated == []
    assert cleaned == [frozenset()]
