from __future__ import annotations

from openbase_coder_cli.services import network


class _FakeAddr:
    def __init__(self, address: str) -> None:
        self.address = address


def test_resolve_interface_returns_matching_interface_name(monkeypatch):
    monkeypatch.setattr(
        network.psutil,
        "net_if_addrs",
        lambda: {
            "lo0": [_FakeAddr("127.0.0.1")],
            "en0": [_FakeAddr("100.64.1.2")],
        },
    )

    assert network.resolve_interface("100.64.1.2") == "en0"


def test_resolve_interface_returns_none_when_no_match(monkeypatch):
    monkeypatch.setattr(
        network.psutil,
        "net_if_addrs",
        lambda: {"lo0": [_FakeAddr("127.0.0.1")]},
    )

    assert network.resolve_interface("100.64.1.2") is None


def test_resolve_interface_returns_none_for_invalid_ip():
    assert network.resolve_interface("not-an-ip") is None


def test_tailscale_ip_reads_active_provider_status(monkeypatch):
    from openbase_coder_cli.services import tailscale_provider as tp

    monkeypatch.setattr(
        tp,
        "status_json",
        lambda: {"Self": {"TailscaleIPs": ["100.64.1.2", "fd7a:115c:a1e0::2"]}},
    )

    assert network.tailscale_ip("4") == "100.64.1.2"
    assert network.tailscale_ip("6") == "fd7a:115c:a1e0::2"


def test_tailscale_ip_returns_none_when_provider_errors(monkeypatch):
    from openbase_coder_cli.services import tailscale_provider as tp

    monkeypatch.setattr(tp, "status_json", lambda: {"error": "not running"})

    assert network.tailscale_ip("4") is None


def test_default_lan_ip_returns_socket_bound_address(monkeypatch):
    class _FakeSocket:
        def connect(self, addr):
            return None

        def getsockname(self):
            return ("192.168.1.20", 0)

        def close(self):
            return None

    monkeypatch.setattr(network.socket, "socket", lambda *a, **k: _FakeSocket())

    assert network.default_lan_ip() == "192.168.1.20"


def test_default_lan_ip_returns_none_on_socket_error(monkeypatch):
    class _FakeSocket:
        def connect(self, addr):
            raise OSError("network unreachable")

        def close(self):
            return None

    monkeypatch.setattr(network.socket, "socket", lambda *a, **k: _FakeSocket())

    assert network.default_lan_ip() is None
