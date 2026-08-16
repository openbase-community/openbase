from __future__ import annotations

import psutil

from openbase_coder_cli.services import process_utils


class _FakeAddr:
    def __init__(self, port: int) -> None:
        self.port = port


class _FakeConn:
    def __init__(self, port: int, pid: int | None, status: str = psutil.CONN_LISTEN) -> None:
        self.laddr = _FakeAddr(port)
        self.pid = pid
        self.status = status


def test_listening_pids_returns_pids_matching_port_and_listen_state(monkeypatch):
    conns = [
        _FakeConn(7880, 111),
        _FakeConn(7880, 222, status=psutil.CONN_ESTABLISHED),
        _FakeConn(4500, 333),
    ]
    monkeypatch.setattr(
        process_utils.psutil, "net_connections", lambda kind="inet": conns
    )

    assert process_utils.listening_pids(7880) == {111}


def test_listening_pids_returns_empty_set_when_no_match(monkeypatch):
    monkeypatch.setattr(
        process_utils.psutil, "net_connections", lambda kind="inet": []
    )

    assert process_utils.listening_pids(9999) == set()


class _FakeProcess:
    def __init__(
        self,
        pid: int,
        cmdline: list[str] | None = None,
        raise_access: bool = False,
    ) -> None:
        self.pid = pid
        self._cmdline = cmdline or []
        self._raise_access = raise_access
        self.terminated = False
        self.killed = False

    def cmdline(self) -> list[str]:
        if self._raise_access:
            raise psutil.AccessDenied()
        return self._cmdline

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_process_cmdline_joins_argv(monkeypatch):
    fake = _FakeProcess(555, cmdline=["/usr/bin/python3", "-m", "livekit"])
    monkeypatch.setattr(process_utils.psutil, "Process", lambda pid: fake)

    assert process_utils.process_cmdline(555) == "/usr/bin/python3 -m livekit"


def test_process_cmdline_returns_empty_string_when_process_missing(monkeypatch):
    def raise_no_such(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(process_utils.psutil, "Process", raise_no_such)

    assert process_utils.process_cmdline(999) == ""


def test_process_cmdline_returns_empty_string_on_access_denied(monkeypatch):
    fake = _FakeProcess(555, raise_access=True)
    monkeypatch.setattr(process_utils.psutil, "Process", lambda pid: fake)

    assert process_utils.process_cmdline(555) == ""


def test_terminate_uses_os_kill_sigterm_on_posix(monkeypatch):
    calls = []
    monkeypatch.setattr(process_utils.sys, "platform", "linux")
    monkeypatch.setattr(
        process_utils.os, "kill", lambda pid, sig: calls.append((pid, sig))
    )

    process_utils.terminate(123)

    assert calls == [(123, process_utils._SIGTERM)]


def test_terminate_uses_os_kill_sigkill_on_posix_when_forced(monkeypatch):
    calls = []
    monkeypatch.setattr(process_utils.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_utils.os, "kill", lambda pid, sig: calls.append((pid, sig))
    )

    process_utils.terminate(123, force=True)

    assert calls == [(123, process_utils._SIGKILL)]


def test_terminate_swallows_missing_process_on_posix(monkeypatch):
    monkeypatch.setattr(process_utils.sys, "platform", "linux")

    def raise_lookup(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(process_utils.os, "kill", raise_lookup)

    process_utils.terminate(999)  # must not raise


def test_terminate_uses_psutil_terminate_on_windows(monkeypatch):
    fake = _FakeProcess(321)
    monkeypatch.setattr(process_utils.sys, "platform", "win32")
    monkeypatch.setattr(process_utils.psutil, "Process", lambda pid: fake)

    process_utils.terminate(321)

    assert fake.terminated is True
    assert fake.killed is False


def test_terminate_uses_psutil_kill_on_windows_when_forced(monkeypatch):
    fake = _FakeProcess(321)
    monkeypatch.setattr(process_utils.sys, "platform", "win32")
    monkeypatch.setattr(process_utils.psutil, "Process", lambda pid: fake)

    process_utils.terminate(321, force=True)

    assert fake.killed is True
    assert fake.terminated is False


def test_terminate_swallows_missing_process_on_windows(monkeypatch):
    monkeypatch.setattr(process_utils.sys, "platform", "win32")

    def raise_no_such(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(process_utils.psutil, "Process", raise_no_such)

    process_utils.terminate(999)  # must not raise
