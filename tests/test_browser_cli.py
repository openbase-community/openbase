from __future__ import annotations

import importlib
import shlex

import click
import httpx
from click.testing import CliRunner

browser_cli = importlib.import_module("openbase_coder_cli.cli.browser")
local_server = importlib.import_module("openbase_coder_cli.cli.local_server")
main_cli = importlib.import_module("openbase_coder_cli.cli")

LOGIN_URL = "https://auth.example.com/authorize?client_id=cli&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fcallback"


def _patch_publish(monkeypatch, result):
    calls = []

    def publish(url):
        calls.append(url)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(browser_cli, "publish_open_url", publish)
    return calls


def test_browser_open_reports_delivery(monkeypatch):
    calls = _patch_publish(monkeypatch, {"command_id": "c-1", "delivered": True})

    result = CliRunner().invoke(browser_cli.browser, ["open", LOGIN_URL])

    assert result.exit_code == 0
    assert result.output.splitlines() == [LOGIN_URL, browser_cli.OPENED_MESSAGE]
    assert calls == [LOGIN_URL]


def test_browser_open_prints_paste_back_hint_when_not_delivered(monkeypatch):
    _patch_publish(monkeypatch, {"command_id": "c-1", "delivered": False})

    result = CliRunner().invoke(browser_cli.browser, ["open", LOGIN_URL])

    assert result.exit_code == 0
    assert result.output.splitlines() == [LOGIN_URL, browser_cli.NOT_DELIVERED_HINT]
    assert "paste that final address back here" in result.output


def test_browser_open_succeeds_when_local_server_is_unreachable(monkeypatch):
    def refuse(method, url, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(local_server, "get_local_api_token", lambda: "local-token")
    monkeypatch.setattr(local_server.httpx, "request", refuse)

    result = CliRunner().invoke(browser_cli.browser, ["open", LOGIN_URL])

    assert result.exit_code == 0
    assert result.output.splitlines() == [LOGIN_URL, browser_cli.NOT_DELIVERED_HINT]


def test_browser_open_posts_open_url_to_the_local_app_control_api(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs["json"]))
        return httpx.Response(202, json={"command_id": "c-1", "delivered": True})

    monkeypatch.delenv("OPENBASE_CODER_CLI_SERVER_URL", raising=False)
    monkeypatch.delenv("OPENBASE_CODER_CLI_LOCAL_SERVER_URL", raising=False)
    monkeypatch.delenv("OPENBASE_CODER_CLI_HOST", raising=False)
    monkeypatch.delenv("OPENBASE_CODER_CLI_PORT", raising=False)
    monkeypatch.setattr(local_server, "get_local_api_token", lambda: "local-token")
    monkeypatch.setattr(local_server.httpx, "request", fake_request)

    result = CliRunner().invoke(
        browser_cli.browser, ["open", "--no-forward", LOGIN_URL]
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:7999/api/user/ios-app-control/",
            {"action": "open_url", "url": LOGIN_URL},
        )
    ]


def test_browser_open_treats_server_errors_as_not_delivered(monkeypatch):
    _patch_publish(
        monkeypatch, click.ClickException("Channel layer is not configured.")
    )

    result = CliRunner().invoke(browser_cli.browser, ["open", LOGIN_URL])

    assert result.exit_code == 0
    assert browser_cli.NOT_DELIVERED_HINT in result.output


def test_browser_open_rejects_disallowed_urls_without_publishing(monkeypatch):
    calls = _patch_publish(monkeypatch, {"delivered": True})

    for url in ("javascript:alert(1)", "file:///etc/passwd", "no-scheme"):
        result = CliRunner().invoke(browser_cli.browser, ["open", url])
        assert result.exit_code != 0, url
        assert url not in result.output.splitlines()

    assert calls == []


def test_browser_handler_invocation_with_url_as_only_argument(monkeypatch):
    calls = _patch_publish(monkeypatch, {"delivered": True})
    browser_env = "openbase-coder browser open"
    argv = shlex.split(browser_env)[1:] + [LOGIN_URL]

    result = CliRunner().invoke(main_cli.main, argv)

    assert result.exit_code == 0
    assert result.output.splitlines()[0] == LOGIN_URL
    assert calls == [LOGIN_URL]


def test_browser_open_help_documents_no_forward_as_a_no_op():
    result = CliRunner().invoke(browser_cli.browser, ["open", "--help"])

    assert result.exit_code == 0
    assert "--no-forward" in result.output
    assert "no-op" in result.output
