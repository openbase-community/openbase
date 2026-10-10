"""The container entrypoint keeps a size-capped ``<logs>/<service>.log`` per
supervised service, as launchd and systemd installs do, while the console
stream stays intact (and intact even when the sink cannot run)."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ENTRYPOINT = Path(__file__).parents[1] / "docker" / "entrypoint.sh"
REPO_ROOT = Path(__file__).parents[1]


def _function(name: str) -> str:
    match = re.search(
        rf"^{name}\(\) \{{\n.*?^\}}\n", ENTRYPOINT.read_text(), re.M | re.S
    )
    assert match, f"{name}() not found in entrypoint.sh"
    return match.group(0)


def _run_supervised(tmp_path: Path, *, with_python: bool, log_dir: Path | None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if with_python:
        # A shell wrapper, not a symlink: a symlinked venv python loses its
        # pyvenv.cfg and with it the installed packages.
        wrapper = bin_dir / "python"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        wrapper.chmod(0o755)
    script = (
        "set -euo pipefail\n"
        + _function("service_log_sink")
        + _function("start_supervised")
        + "start_supervised demo bash -c 'echo \"hello from service\"; sleep 300'\n"
        + "wait\n"
    )
    env = {
        **{k: v for k, v in os.environ.items() if k not in {"LOG_DIR"}},
        "RUN_DIR": str(tmp_path),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT),
    }
    if log_dir is not None:
        env["LOG_DIR"] = str(log_dir)
    return subprocess.Popen(
        ["bash", "-c", script],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )


def _wait_for(predicate, *, what: str, proc) -> None:
    deadline = time.monotonic() + 15
    while not predicate():
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


def _finish(proc) -> tuple[str, str]:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return proc.communicate(timeout=10)


def test_entrypoint_defines_the_log_dir_next_to_the_run_dir():
    text = ENTRYPOINT.read_text()
    assert 'LOG_DIR="$DATA_DIR/logs"' in text
    assert '| service_log_sink "$name" |' in text


def test_service_output_is_mirrored_into_a_log_file(tmp_path):
    log_dir = tmp_path / "logs"
    proc = _run_supervised(tmp_path, with_python=True, log_dir=log_dir)
    log = log_dir / "demo.log"
    try:
        _wait_for(
            lambda: log.exists() and "hello from service" in log.read_text(),
            what="service line never reached the log file",
            proc=proc,
        )
    finally:
        stdout, stderr = _finish(proc)

    assert log.read_text() == "hello from service\n"
    assert "[demo] hello from service" in stdout
    assert "[log-sink]" not in stderr


def test_console_stream_survives_a_missing_sink(tmp_path):
    log_dir = tmp_path / "logs"
    proc = _run_supervised(tmp_path, with_python=False, log_dir=log_dir)
    seen: list[str] = []
    try:
        deadline = time.monotonic() + 15
        os.set_blocking(proc.stdout.fileno(), False)
        while not any("hello from service" in line for line in seen):
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "console line never arrived"
            try:
                chunk = os.read(proc.stdout.fileno(), 4096)
            except BlockingIOError:
                chunk = b""
            if chunk:
                seen.append(chunk.decode(errors="replace"))
            time.sleep(0.05)
    finally:
        _finish(proc)

    assert any("[demo] hello from service" in line for line in seen)
    assert not (log_dir / "demo.log").exists()


def test_stream_passes_through_without_a_log_dir(tmp_path):
    proc = _run_supervised(tmp_path, with_python=True, log_dir=None)
    seen: list[str] = []
    try:
        deadline = time.monotonic() + 15
        os.set_blocking(proc.stdout.fileno(), False)
        while not any("hello from service" in line for line in seen):
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "console line never arrived"
            try:
                chunk = os.read(proc.stdout.fileno(), 4096)
            except BlockingIOError:
                chunk = b""
            if chunk:
                seen.append(chunk.decode(errors="replace"))
            time.sleep(0.05)
    finally:
        _finish(proc)

    assert any("[demo] hello from service" in line for line in seen)
    assert not list(tmp_path.glob("**/*.log"))
