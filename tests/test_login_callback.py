from __future__ import annotations

import pytest

from openbase_coder_cli.login_callback import LoopbackForward, loopback_callback_port


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
            2000,
        ),
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
