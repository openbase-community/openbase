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


def test_stop_leaves_a_marker_and_reports_the_service_stopped(external, monkeypatch):
    """A stop is a stop, not a bounce: the marker keeps the relaunched wrapper
    idle, and status reads the service as installed but not running."""
    svc, pidfile, terminated, _cleaned = external
    monkeypatch.setattr(launchd, "_cleanup_service_endpoint", lambda _svc: None)

    assert launchd.launchctl_bootout(svc) is True
    assert terminated == [(4242, False)]
    marker = launchd.external_supervisor_stop_marker(svc)
    assert marker.exists()

    # The entrypoint relaunches the wrapper, which idles on the marker.
    pidfile.write_text("4243", encoding="utf-8")
    assert launchd.launchctl_status(svc) == {"installed": True, "pid": None}
    assert launchd.launchctl_bootout(svc) is True  # idle wrapper ended too

    pidfile.unlink(missing_ok=True)
    assert launchd.launchctl_bootout(svc) is False


def test_start_clears_the_marker_and_adopts_an_unsupervised_service(
    external, monkeypatch
):
    """Nothing runs the dispatcher app server after a switch to Codex (the
    entrypoint never did); a start spawns the fallback supervisor for it."""
    from openbase_coder_cli.services import external_supervisor

    svc, pidfile, terminated, _cleaned = external
    pidfile.unlink()
    launchd.external_supervisor_stop_marker(svc).touch()
    spawned: list[str] = []
    monkeypatch.setattr(launchd, "EXTERNAL_SUPERVISOR_RELAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(launchd, "_prepare_service_start", lambda _svc: None)
    monkeypatch.setattr(external_supervisor, "supervisor_pid", lambda _name: None)
    monkeypatch.setattr(
        external_supervisor, "spawn_supervisor", lambda name: spawned.append(name)
    )

    launchd.launchctl_bootstrap(svc)

    assert spawned == [svc.name]
    assert terminated == []
    assert not launchd.external_supervisor_stop_marker(svc).exists()


def test_start_defers_to_a_supervisor_that_is_between_relaunches(external, monkeypatch):
    from openbase_coder_cli.services import external_supervisor

    svc, pidfile, _terminated, _cleaned = external
    pidfile.unlink()
    spawned: list[str] = []
    monkeypatch.setattr(launchd, "EXTERNAL_SUPERVISOR_RELAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(external_supervisor, "supervisor_pid", lambda _name: 77)
    monkeypatch.setattr(
        external_supervisor, "spawn_supervisor", lambda name: spawned.append(name)
    )

    launchd.launchctl_bootstrap(svc)

    assert spawned == []


def test_start_of_a_marker_stopped_service_ends_the_idle_wrapper(external):
    """The entrypoint still loops a stopped service's wrapper, idling on the
    marker; a start clears the marker and bounces it so the relaunch serves."""
    svc, _pidfile, terminated, _cleaned = external
    launchd.external_supervisor_stop_marker(svc).touch()

    assert launchd.launchctl_restart(svc) is True

    assert terminated == [(4242, False)]
    assert not launchd.external_supervisor_stop_marker(svc).exists()
