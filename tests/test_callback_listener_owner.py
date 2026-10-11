"""Callback discovery runs without global socket privileges on macOS."""

import os
import subprocess
import sys
from types import SimpleNamespace as N

import psutil
import pytest

from openbase_coder_cli.callback_relay import ListenerOwner


def test_discover_real_child_listener_and_retire_after_exit():
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import socket,sys; s=socket.socket(); s.bind(('127.0.0.1',0)); "
            "s.listen(); print(s.getsockname()[1],flush=True); sys.stdin.read()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        port = int(child.stdout.readline())
        owner = ListenerOwner.find(port)
        assert owner.pid == child.pid and owner.host == "127.0.0.1"
        assert owner.fd >= 0 and owner.alive()
    finally:
        child.stdin.close()
        child.wait(timeout=3)
        child.stdout.close()
    assert not owner.alive()
    with pytest.raises(ValueError, match="loopback-only"):
        ListenerOwner.find(port)


def process(pid, *, host="127.0.0.1", uid=None, fd=5, error=None):
    def connections(**kwargs):
        if error:
            raise error
        return [N(status=psutil.CONN_LISTEN, laddr=N(ip=host, port=8085), fd=fd)]

    return N(
        pid=pid,
        uids=lambda: N(real=os.getuid() if uid is None else uid),
        net_connections=connections,
        create_time=lambda: 10,
    )


@pytest.fixture
def mac_processes(monkeypatch):
    monkeypatch.setattr("openbase_coder_cli.callback_relay.sys.platform", "darwin")

    def forbidden(**kwargs):
        raise AssertionError("macOS must not enumerate privileged system sockets")

    monkeypatch.setattr(psutil, "net_connections", forbidden)

    def install(processes):
        monkeypatch.setattr(psutil, "process_iter", lambda: iter(processes))
        monkeypatch.setattr(
            psutil, "Process", lambda pid: next(p for p in processes if p.pid == pid)
        )

    return install


def test_mac_ignores_inaccessible_and_foreign_processes(mac_processes):
    mac_processes(
        [
            process(1, error=psutil.AccessDenied(1)),
            process(2, error=psutil.NoSuchProcess(2)),
            process(3, uid=os.getuid() + 1, error=AssertionError("foreign inspection")),
            process(4),
        ]
    )
    assert ListenerOwner.find(8085) == ListenerOwner(4, 10, 5, "127.0.0.1", 8085)


@pytest.mark.parametrize(
    "processes,reason",
    [
        ([process(1, host="0.0.0.0")], "loopback-only"),
        ([process(1), process(2, host="::1")], "ambiguous"),
        ([process(1, error=psutil.AccessDenied(1))], "loopback-only"),
        ([process(1, uid=os.getuid() + 1)], "loopback-only"),
        ([process(1, fd=-1)], "socket identity"),
    ],
)
def test_mac_refuses_unverified_owners(mac_processes, processes, reason):
    mac_processes(processes)
    with pytest.raises(ValueError, match=reason):
        ListenerOwner.find(8085)
