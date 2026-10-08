"""Real process/thread contention and explicit updater-child delegation."""

import os
import subprocess
import sys
import threading

import pytest

from openbase_coder_cli.services import mutation_lock as lock


@pytest.fixture(autouse=True)
def isolated_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(lock, "LOCK_PATH", tmp_path / "mutation.lock")
    monkeypatch.delenv(lock.DELEGATION_ENV, raising=False)


def child(environment):
    script = """
import sys
from pathlib import Path
from openbase_coder_cli.services import mutation_lock as lock
lock.LOCK_PATH = Path(sys.argv[1])
try:
    with lock.service_mutation(timeout=0.15):
        print("entered")
except lock.ServiceMutationBusy:
    print("busy")
"""
    return subprocess.run(
        [sys.executable, "-c", script, str(lock.LOCK_PATH)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    ).stdout.strip()


def test_updater_child_can_install_but_unrelated_process_is_excluded():
    with lock.service_mutation():
        assert child(dict(os.environ)) == "busy"
        assert child(lock.mutation_environment()) == "entered"
        assert child(dict(os.environ)) == "busy"  # child did not release parent's lock
    assert child(dict(os.environ)) == "entered"


def test_stale_delegation_cannot_skip_a_new_owner():
    with lock.service_mutation():
        stale = lock.mutation_environment()
    with lock.service_mutation():
        assert child(stale) == "busy"


def test_reentrant_owner_excludes_other_threads_and_releases_after_error():
    outcomes = []

    def contender():
        try:
            with lock.service_mutation(timeout=0.05):
                outcomes.append("entered")
        except lock.ServiceMutationBusy:
            outcomes.append("busy")

    with pytest.raises(RuntimeError, match="activation failed"):
        with lock.service_mutation():
            with lock.service_mutation():
                thread = threading.Thread(target=contender)
                thread.start()
                thread.join(timeout=2)
                assert not thread.is_alive()
                assert outcomes == ["busy"]
                raise RuntimeError("activation failed")
    contender()
    assert outcomes == ["busy", "entered"]


def test_killed_owner_releases_os_lock(tmp_path):
    marker = tmp_path / "ready"
    script = """
import sys, time
from pathlib import Path
from openbase_coder_cli.services import mutation_lock as lock
lock.LOCK_PATH = Path(sys.argv[1])
with lock.service_mutation():
    print("ready", flush=True)
    time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(lock.LOCK_PATH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        import selectors

        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5)
            assert process.stdout.readline().strip() == "ready"
        process.kill()
        process.wait(timeout=5)
        with lock.service_mutation(timeout=0.1):
            marker.write_text("recovered")
        assert marker.read_text() == "recovered"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_old_runtime_cannot_mutate_services_after_another_update(monkeypatch, tmp_path):
    import click

    from openbase_coder_cli import paths, runtime
    from openbase_coder_cli._version import __version__

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    current = tmp_path / "current"
    current.symlink_to(new)
    monkeypatch.setattr(paths, "STANDALONE_CURRENT_DIR", current)
    monkeypatch.setattr(
        runtime,
        "current_runtime_package",
        lambda: runtime.RuntimePackage(root=old, version=__version__),
    )
    with pytest.raises(click.ClickException, match="older runtime"):
        with lock.service_mutation():
            pytest.fail("stale restart entered activation")
    assert lock.LOCK_PATH.read_text() == ""


def test_real_updater_launcher_delegates_the_held_lease(tmp_path):
    from openbase_coder_cli import self_update

    launcher = tmp_path / "launcher"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "from openbase_coder_cli.services import mutation_lock as lock\n"
        f"lock.LOCK_PATH = Path({str(lock.LOCK_PATH)!r})\n"
        "with lock.service_mutation(timeout=0.1):\n"
        "    print('services activated')\n"
    )
    launcher.chmod(0o755)
    with lock.service_mutation():
        assert self_update._run_launcher(
            launcher, ["services", "install"], report=lambda _: None
        )
        assert child(dict(os.environ)) == "busy"


@pytest.mark.skipif(os.name == "nt", reason="POSIX inherited flock contract")
def test_service_child_keeps_lease_after_updater_is_killed(tmp_path):
    import signal
    import time

    ready = tmp_path / "child-ready"
    release = tmp_path / "release-child"
    child_code = """
import os, sys, time
from pathlib import Path
from openbase_coder_cli.services import mutation_lock as lock
lock.LOCK_PATH = Path(sys.argv[1])
ready = Path(sys.argv[2])
ready.write_text(str(os.getpid()))
while not ready.with_suffix(".enter").exists():
    time.sleep(0.02)
with lock.service_mutation(timeout=0.1):
    ready.with_suffix(".entered").touch()
    while not Path(sys.argv[3]).exists():
        time.sleep(0.02)
"""
    parent_code = """
import subprocess, sys, time
from pathlib import Path
from openbase_coder_cli.services import mutation_lock as lock
lock.LOCK_PATH = Path(sys.argv[1])
with lock.service_mutation():
    process = subprocess.Popen([sys.executable, "-c", sys.argv[4], *sys.argv[1:4]],
                               env=lock.mutation_environment(), pass_fds=lock.mutation_descriptors())
    process.wait()
"""
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            parent_code,
            str(lock.LOCK_PATH),
            str(ready),
            str(release),
            child_code,
        ]
    )
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child_pid = int(ready.read_text())
        parent.kill()
        parent.wait(timeout=5)
        assert parent.returncode == -signal.SIGKILL
        assert child(dict(os.environ)) == "busy"
        ready.with_suffix(".enter").touch()
        deadline = time.monotonic() + 5
        while not ready.with_suffix(".entered").exists():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert child(dict(os.environ)) == "busy"
        release.touch()
        deadline = time.monotonic() + 5
        while child(dict(os.environ)) != "entered":
            assert time.monotonic() < deadline
    finally:
        ready.with_suffix(".enter").touch()
        release.touch()
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
