import importlib
import os
import signal
import sys
import threading
import time

import pytest

server = importlib.import_module("openbase_coder_cli.cli.server")


def test_stop_signal_kills_a_server_that_ignores_sigterm(monkeypatch):
    monkeypatch.setattr(server, "STOP_KILL_AFTER_SECONDS", 0.5)
    stubborn = [
        sys.executable,
        "-c",
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(60)",
    ]
    threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM)).start()
    started = time.monotonic()
    with pytest.raises(SystemExit):
        server._run_supervised(stubborn)
    assert time.monotonic() - started < 10


def test_failing_server_exit_raises():
    with pytest.raises(server.subprocess.CalledProcessError):
        server._run_supervised([sys.executable, "-c", "raise SystemExit(3)"])
