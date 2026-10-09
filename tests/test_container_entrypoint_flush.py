"""Container entrypoint durability: page-cache writeback tuning and the
flush-on-stop shutdown trap.

A Maritime stop halts the VM without flushing the page cache, so a file
written in the ~30 s before the stop came back 0 bytes (2026-10-09). These
tests run the entrypoint's own shell functions under bash with stubbed
`sync` and proc paths.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).parents[1] / "docker" / "entrypoint.sh"


def _entrypoint() -> str:
    return ENTRYPOINT.read_text()


def _function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", _entrypoint(), re.M | re.S)
    assert match, f"{name}() not found in entrypoint.sh"
    return match.group(0)


def _run_tune(
    vm_dir: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    script = (
        "set -euo pipefail\n"
        + _function("tune_writeback")
        + 'tune_writeback "$1"\necho CONTINUED\n'
    )
    run_env = {
        k: v for k, v in os.environ.items() if not k.startswith("OPENBASE_DIRTY_")
    }
    run_env.update(env or {})
    return subprocess.run(
        ["bash", "-c", script, "tune", str(vm_dir)],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=10,
    )


def _vm_dir(tmp_path: Path) -> Path:
    vm_dir = tmp_path / "vm"
    vm_dir.mkdir()
    (vm_dir / "dirty_expire_centisecs").write_text("3000\n")
    (vm_dir / "dirty_writeback_centisecs").write_text("500\n")
    return vm_dir


def test_tune_writeback_writes_short_window_by_default(tmp_path):
    vm_dir = _vm_dir(tmp_path)

    result = _run_tune(vm_dir)

    assert result.returncode == 0, result.stderr
    assert (vm_dir / "dirty_expire_centisecs").read_text() == "500\n"
    assert (vm_dir / "dirty_writeback_centisecs").read_text() == "100\n"
    assert "CONTINUED" in result.stdout


def test_tune_writeback_values_are_overridable(tmp_path):
    vm_dir = _vm_dir(tmp_path)

    result = _run_tune(
        vm_dir,
        {
            "OPENBASE_DIRTY_EXPIRE_CENTISECS": "200",
            "OPENBASE_DIRTY_WRITEBACK_CENTISECS": "50",
        },
    )

    assert result.returncode == 0, result.stderr
    assert (vm_dir / "dirty_expire_centisecs").read_text() == "200\n"
    assert (vm_dir / "dirty_writeback_centisecs").read_text() == "50\n"


def test_tune_writeback_failure_is_tolerated(tmp_path):
    result = _run_tune(tmp_path / "read-only-proc-sys-vm")

    assert result.returncode == 0, result.stderr
    assert "CONTINUED" in result.stdout
    assert result.stderr.count("\n") == 1
    assert "Could not fully set the page-cache writeback window" in result.stderr


def test_tune_writeback_rejects_non_numeric_values(tmp_path):
    vm_dir = _vm_dir(tmp_path)

    result = _run_tune(vm_dir, {"OPENBASE_DIRTY_EXPIRE_CENTISECS": "5; rm -rf /"})

    assert result.returncode == 0, result.stderr
    assert "CONTINUED" in result.stdout
    assert (vm_dir / "dirty_expire_centisecs").read_text() == "3000\n"
    assert (vm_dir / "dirty_writeback_centisecs").read_text() == "500\n"


def test_tune_writeback_runs_as_root_on_maritime_and_early_elsewhere():
    entrypoint = _entrypoint()
    root_block = entrypoint.index(
        'if [ "${OPENBASE_CODER_RUNTIME:-}" = "maritime" ] && [ "$(/usr/bin/id -u)" = "0" ]'
    )
    privilege_drop = entrypoint.index("exec /usr/bin/setpriv")
    calls = [m.start() for m in re.finditer(r"^\s*tune_writeback$", entrypoint, re.M)]

    assert len(calls) == 2
    assert root_block < calls[0] < privilege_drop
    assert calls[1] < entrypoint.index("# --- First-run setup")
    assert (
        'if [ "${OPENBASE_CODER_RUNTIME:-}" != "maritime" ]; then\n    tune_writeback\nfi'
        in entrypoint
    )


def test_shutdown_trap_syncs_after_services_exit(tmp_path):
    marker = tmp_path / "sync-marker"
    ready = tmp_path / "ready"
    script = (
        "set -euo pipefail\n"
        # Stub sync: record whether the service was still alive when it ran.
        f'sync() {{ if kill -0 "$svc" 2>/dev/null; then echo early >"{marker}"; else echo synced >"{marker}"; fi; }}\n'
        + _function("shutdown")
        + "trap shutdown TERM INT\n"
        + "sleep 300 &\nsvc=$!\n"
        + f'touch "{ready}"\n'
        + "wait\n"
        + f'echo fell-through >"{marker}"\n'
    )
    # A new session keeps the trap's `kill 0` away from the test runner.
    proc = subprocess.Popen(
        ["bash", "-c", script],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < deadline, "harness never became ready"
            time.sleep(0.05)
        os.kill(proc.pid, signal.SIGTERM)
        _, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    assert proc.returncode == 0, stderr
    assert marker.read_text() == "synced\n"
    assert "flushing filesystem buffers" in stderr


@pytest.mark.parametrize("supervised", [False, True])
@pytest.mark.parametrize("stubborn", [False, True])
def test_shutdown_flushes_before_wait_and_after_final_write(
    tmp_path, supervised, stubborn
):
    marker = tmp_path / "sync-marker"
    ready = tmp_path / "ready"
    stopped = tmp_path / "stopped"
    service = tmp_path / "service.py"
    service.write_text(
        "import os, signal, sys, time\n"
        "from pathlib import Path\n"
        "def stop(signum, frame):\n"
        "    time.sleep(0.3)\n"
        "    print('service shutdown complete', flush=True)\n"
        "    Path(os.environ['STOPPED']).write_text('final write')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN if os.environ['STUBBORN'] == '1' else stop)\n"
        "Path(os.environ['READY']).touch()\n"
        "while True: time.sleep(1)\n"
    )
    script = (
        "set -euo pipefail\n"
        'sync() { if [ -f "$STOPPED" ]; then echo stopped >>"$MARKER"; else echo running >>"$MARKER"; fi; }\n'
        + _function("start_supervised")
        + _function("shutdown")
        + "trap shutdown TERM INT\n"
        + ('start_supervised delayed "$1" "$2"\n' if supervised else '"$1" "$2" &\n')
        + "wait\n"
    )
    proc = subprocess.Popen(
        ["bash", "-c", script, "shutdown", sys.executable, str(service)],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "RUN_DIR": str(tmp_path),
            "READY": str(ready),
            "STOPPED": str(stopped),
            "MARKER": str(marker),
            "STUBBORN": "1" if stubborn else "0",
        },
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "service never became ready"
            time.sleep(0.05)
        os.kill(proc.pid, signal.SIGTERM)
        if stubborn:
            while not marker.exists():
                assert time.monotonic() < deadline, "no flush before waiting"
                time.sleep(0.05)
            assert marker.read_text().splitlines() == ["running"]
            assert proc.poll() is None
            assert not stopped.exists()
        else:
            stdout, stderr = proc.communicate(timeout=10)
            assert proc.returncode == 0, stderr
            assert marker.read_text().splitlines() == ["running", "stopped"]
            assert "service shutdown complete" in stdout
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
