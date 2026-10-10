from __future__ import annotations

import os

import httpx
import pytest

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli.login_callback import loopback_replay_target  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    oauth_callback_replay as replay,
)

CALLBACK = "http://localhost:1455/auth/callback?code=abc&state=xyz#frag"


@pytest.mark.parametrize(
    ("pasted", "expected"),
    [
        (CALLBACK, (1455, "/auth/callback?code=abc&state=xyz")),
        (
            "http://127.0.0.1:52807/oauth/callback?code=1",
            (52807, "/oauth/callback?code=1"),
        ),
        ("http://[::1]:8085/", (8085, "/")),
        ("  http://localhost:3000  ", (3000, "/")),
        ("https://localhost:1455/cb", None),
        ("http://localhost/cb", None),
        ("http://localhost:80/cb", None),
        ("http://evil.example:1455/cb", None),
        ("http://0.0.0.0:1455/cb", None),
        ("http://localhost:1455/cb\x00", None),
        ("not a url", None),
    ],
)
def test_loopback_replay_target(pasted, expected):
    target = loopback_replay_target(pasted)
    if expected is None:
        assert target is None
    else:
        assert (target.port, target.path) == expected
        assert target.url == f"http://127.0.0.1:{expected[0]}{expected[1]}"


def _post(data, authenticated=True):
    request = APIRequestFactory().post(
        "/api/user/oauth-callback-replay/", data, format="json"
    )
    if authenticated:
        force_authenticate(request, user=type("U", (), {"is_authenticated": True})())
    return replay.oauth_callback_replay(request)


def test_replay_gets_the_callback_on_loopback_without_following_redirects(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(url=url, kwargs=kwargs)
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(replay.httpx, "get", fake_get)
    response = _post({"url": CALLBACK})

    assert response.status_code == 200
    assert response.data == {"ok": True, "port": 1455, "status_code": 200}
    assert seen["url"] == "http://127.0.0.1:1455/auth/callback?code=abc&state=xyz"
    assert seen["kwargs"]["follow_redirects"] is False


def test_replay_reports_a_closed_port(monkeypatch):
    def refuse(url, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(replay.httpx, "get", refuse)
    response = _post({"url": CALLBACK})

    assert response.status_code == 502
    assert response.data["ok"] is False
    assert response.data["port"] == 1455
    assert "ConnectError" in response.data["error"]
    assert "code=abc" not in str(response.data)


def test_replay_reports_a_rejected_callback(monkeypatch):
    monkeypatch.setattr(
        replay.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(400, request=httpx.Request("GET", url)),
    )
    response = _post({"url": CALLBACK})

    assert response.status_code == 502
    assert response.data == {"ok": False, "port": 1455, "status_code": 400}


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/steal?code=1",
        "http://localhost/cb",
        "http://10.0.0.5:1455/cb",
        "",
    ],
)
def test_replay_rejects_non_loopback_addresses(monkeypatch, url):
    called = []
    monkeypatch.setattr(replay.httpx, "get", lambda *a, **k: called.append(a))
    response = _post({"url": url})

    assert response.status_code == 400
    assert called == []
