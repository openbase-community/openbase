from __future__ import annotations

import pytest

from openbase_coder_cli.login_callback import (
    LoopbackForward,
    is_tailnet_forward_target,
    loopback_callback_port,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://a.example/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fcallback",
            1455,
        ),
        (
            "https://a.example/authorize?redirect_uri=http://127.0.0.1:52807/oauth/callback",
            52807,
        ),
        ("https://a.example/authorize?redirect_uri=http://[::1]:8085/", 8085),
        ("https://a.example/authorize?redirect_url=http://localhost:3000/cb&x=1", 3000),
        ("https://a.example/authorize?redirect_uri=https://a.example/callback", None),
        ("https://a.example/authorize?redirect_uri=http://localhost/callback", None),
        ("https://a.example/authorize?redirect_uri=http://localhost:80/callback", None),
        ("https://a.example/authorize?redirect_uri=http://localhost:70000/", None),
        (
            "https://a.example/authorize?redirect_uri=custom-scheme://localhost:1455/",
            None,
        ),
        ("https://a.example/device", None),
        (
            "https://a.example/?redirect_uri=http://evil.example:1455@localhost:2000/",
            None,
        ),
        ("http://localhost:8085/start", 8085),
        ("http://0.0.0.0:8085/start", None),
        ("http://user@localhost:8085/start", None),
        ("not a url", None),
    ],
)
def test_loopback_callback_port(url, expected):
    assert loopback_callback_port(url) == expected


def test_loopback_forward_payloads():
    forward = LoopbackForward.create(1455, "100.64.0.12")

    assert forward.ttl_seconds == 600
    assert len(forward.token) >= 24
    assert forward.as_app_control() == {
        "port": 1455,
        "target": "100.64.0.12",
        "ttl_seconds": 600,
        "token": forward.token,
    }
    assert forward.as_push_user_info() == {
        "forward_port": "1455",
        "forward_target": "100.64.0.12",
        "forward_ttl_seconds": "600",
        "forward_token": forward.token,
    }
    assert LoopbackForward.create(1, "x").token != forward.token


@pytest.mark.parametrize(
    "target", ["100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::12"]
)
def test_forward_target_accepts_only_vpn_literals(target):
    assert is_tailnet_forward_target(target)


@pytest.mark.parametrize(
    "target",
    [
        "127.0.0.1",
        "192.168.1.2",
        "8.8.8.8",
        "100.128.0.1",
        "100.63.255.255",
        "::1",
        "2001:db8::1",
        "fd7a:115c:a1e0::12%en0",
        "workspace.net.obs.so",
        "evil.example",
    ],
)
def test_forward_target_rejects_dns_and_non_vpn_addresses(target):
    assert not is_tailnet_forward_target(target)


def test_authenticated_relay_payload_has_protocol_and_absolute_deadline():
    forward = LoopbackForward(
        1455, "100.64.0.12", token="x" * 32, relay_port=49152, expires_at=2000000000
    )
    assert forward.as_app_control()["relay_port"] == 49152
    assert forward.as_app_control()["protocol"] == "OPENBASE-LOOPBACK/1"
    assert forward.as_app_control()["expires_at"] == 2000000000
    assert forward.as_push_user_info()["forward_expires_at"] == "2000000000"
