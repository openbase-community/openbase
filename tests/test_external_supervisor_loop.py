"""The fallback supervisor keeps the entrypoint's pidfile contract."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from openbase_coder_cli.services import external_supervisor as sup


@pytest.fixture
def homed(monkeypatch, tmp_path: Path):
    run_dir = tmp_path / "run"
    wrappers = tmp_path / "launchd"
    logs = tmp_path / "logs"
    wrappers.mkdir()
    monkeypatch.setattr(sup, "RUN_DIR", run_dir)
    monkeypatch.setattr(sup, "LAUNCHD_WRAPPER_DIR", wrappers)
    monkeypatch.setattr(sup, "DEFAULT_LOG_DIR", logs)
    return run_dir, wrappers, logs


def test_supervise_relaunches_until_the_stop_marker_appears(homed):
    run_dir, wrappers, logs = homed
    wrapper = wrappers / "demo.sh"
    wrapper.write_text('#!/bin/bash\necho run >> "$1"\nexit 3\n', encoding="utf-8")
    runs = run_dir / "runs.txt"
    # The supervisor runs `bash <wrapper>`; count runs through the log instead.
    wrapper.write_text(f"#!/bin/bash\necho run >> {runs}\nexit 3\n", encoding="utf-8")
    run_dir.mkdir()

    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(sup.supervise("demo", relaunch_delay=0.05))
    )
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if runs.exists() and len(runs.read_text().splitlines()) >= 2:
            break
        time.sleep(0.02)
    sup.stop_marker("demo").touch()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert result == [0]
    assert len(runs.read_text().splitlines()) >= 2
    assert not sup.service_pidfile("demo").exists()
    assert not sup.supervisor_pidfile("demo").exists()
    assert "exited with status 3" not in (logs / "demo.log").read_text()  # own stdout
    assert (logs / "demo.log").exists()


def test_supervise_publishes_the_service_pid_while_it_runs(homed):
    run_dir, wrappers, _logs = homed
    wrapper = wrappers / "demo.sh"
    wrapper.write_text("#!/bin/bash\nexec sleep 30\n", encoding="utf-8")

    thread = threading.Thread(target=lambda: sup.supervise("demo"))
    thread.start()
    pidfile = sup.service_pidfile("demo")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not pidfile.exists():
        time.sleep(0.02)
    pid = int(pidfile.read_text())
    assert pid > 0
    os.kill(pid, 0)  # alive
    assert int(sup.supervisor_pidfile("demo").read_text()) == os.getpid()
    assert sup.supervisor_pid("demo") == os.getpid()

    sup.stop_marker("demo").touch()
    os.kill(pid, 15)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert not pidfile.exists()
    with pytest.raises(OSError):
        os.kill(pid, 0)


def test_spawn_supervisor_detaches_and_drops_the_mutation_lease(homed, monkeypatch):
    _run_dir, _wrappers, logs = homed
    calls: list[tuple] = []

    class FakePopen:
        pid = 4321

        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

    monkeypatch.setattr(sup.subprocess, "Popen", FakePopen)
    monkeypatch.setenv(sup.DELEGATION_ENV, "lease")
    monkeypatch.setenv(sup.DESCRIPTOR_ENV, "9")

    assert sup.spawn_supervisor("demo") == 4321

    argv, kwargs = calls[0]
    assert argv == [sys.executable, "-m", sup.__name__, "demo"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert sup.DELEGATION_ENV not in kwargs["env"]
    assert sup.DESCRIPTOR_ENV not in kwargs["env"]
    assert (logs / "demo.log").exists()


def test_second_supervisor_cannot_remove_first_supervisors_pidfiles(homed):
    from openbase_coder_cli.file_lock import LOCK_EX, LOCK_NB, flock

    run_dir, _wrappers, _logs = homed
    run_dir.mkdir()
    sup.supervisor_pidfile("demo").write_text(str(os.getpid()))
    sup.service_pidfile("demo").write_text("123")
    with (run_dir / "demo.supervisor.lock").open("a+") as lock:
        flock(lock, LOCK_EX | LOCK_NB)
        assert sup.supervise("demo") == 0
    assert sup.supervisor_pidfile("demo").read_text() == str(os.getpid())
    assert sup.service_pidfile("demo").read_text() == "123"
