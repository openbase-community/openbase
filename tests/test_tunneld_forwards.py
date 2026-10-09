from __future__ import annotations

import httpx
import pytest

from openbase_coder_cli.services import tunneld


class _Response:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


def test_add_forward_posts_body_and_returns_info(monkeypatch):
    seen = {}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, json=json)
        return _Response(201, {"port": 1455, "expires_at": "x"})

    monkeypatch.setattr(tunneld.httpx, "post", fake_post)
    monkeypatch.setattr(tunneld, "_control_headers", lambda: {})
    info = tunneld.tunneld_add_forward(
        1455, ttl_seconds=30, one_shot=True, peer="100.64.0.9"
    )
    assert info["port"] == 1455
    assert seen["url"].endswith("/forwards")
    assert seen["json"] == {
        "port": 1455,
        "one_shot": True,
        "ttl_seconds": 30,
        "peer": "100.64.0.9",
    }


def test_add_forward_raises_daemon_reason(monkeypatch):
    monkeypatch.setattr(
        tunneld.httpx,
        "post",
        lambda *a, **k: _Response(409, {"error": "port 3000 is already forwarded"}),
    )
    monkeypatch.setattr(tunneld, "_control_headers", lambda: {})
    with pytest.raises(tunneld.TunneldForwardError, match="already forwarded"):
        tunneld.tunneld_add_forward(3000)


def test_add_forward_unreachable_daemon(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(tunneld.httpx, "post", boom)
    monkeypatch.setattr(tunneld, "_control_headers", lambda: {})
    with pytest.raises(tunneld.TunneldForwardError, match="not reachable"):
        tunneld.tunneld_add_forward(3000)


def test_remove_forward_status_mapping(monkeypatch):
    monkeypatch.setattr(tunneld, "_control_headers", lambda: {})
    monkeypatch.setattr(tunneld.httpx, "delete", lambda *a, **k: _Response(204))
    assert tunneld.tunneld_remove_forward(3000) is True
    monkeypatch.setattr(tunneld.httpx, "delete", lambda *a, **k: _Response(404))
    assert tunneld.tunneld_remove_forward(3000) is False
    monkeypatch.setattr(
        tunneld.httpx, "delete", lambda *a, **k: _Response(500, {"error": "boom"})
    )
    with pytest.raises(tunneld.TunneldForwardError, match="boom"):
        tunneld.tunneld_remove_forward(3000)


def test_list_forwards_tolerates_daemon_down(monkeypatch):
    monkeypatch.setattr(tunneld, "_control_headers", lambda: {})

    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(tunneld.httpx, "get", boom)
    assert tunneld.tunneld_list_forwards() == []
    monkeypatch.setattr(
        tunneld.httpx,
        "get",
        lambda *a, **k: _Response(200, {"forwards": [{"port": 1}, "junk"]}),
    )
    assert tunneld.tunneld_list_forwards() == [{"port": 1}]


def test_self_dns_name(monkeypatch):
    monkeypatch.setattr(
        tunneld,
        "tunneld_status",
        lambda: (True, {"Self": {"DNSName": "devspace-1.net.obs.so."}}, None),
    )
    assert tunneld.tunneld_self_dns_name() == "devspace-1.net.obs.so"
    monkeypatch.setattr(tunneld, "tunneld_status", lambda: (False, None, "down"))
    assert tunneld.tunneld_self_dns_name() is None
