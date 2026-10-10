from __future__ import annotations

import importlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import click
import httpx
import pytest
from click.testing import CliRunner

browser_cli = importlib.import_module("openbase_coder_cli.cli.browser")
local_server = importlib.import_module("openbase_coder_cli.cli.local_server")
main_cli = importlib.import_module("openbase_coder_cli.cli")

LOGIN_URL = "https://auth.example.com/authorize?client_id=cli&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fcallback"
# A login page with no loopback redirect: nothing to forward, so the output
# is exactly the URL plus one status line.
PLAIN_URL = "https://auth.example.com/device"


@pytest.fixture(autouse=True)
def _no_tailnet_and_no_push(monkeypatch):
    """Default every test to a host without the embedded node and with the
    Cloud push fallback unavailable; tests that need them opt in."""
    from openbase_coder_cli.config import cloud_notifications
    from openbase_coder_cli.services import tailscale_provider

    def no_push(**kwargs):
        raise RuntimeError("push disabled in tests")

    monkeypatch.setattr(tailscale_provider, "is_netmesh_tsnet", lambda: False)
    monkeypatch.setattr(cloud_notifications, "send_notification_push", no_push)


def _patch_publish(monkeypatch, result):
    calls = []

    def publish(url, *, loopback_forward=None):
        calls.append(url if loopback_forward is None else (url, loopback_forward))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(browser_cli, "publish_open_url", publish)
    return calls


def _patch_tunneld(monkeypatch, *, add_error=None, ipv4="100.64.0.12"):
    from openbase_coder_cli.services import tailscale_provider, tunneld

    monkeypatch.setattr(tailscale_provider, "is_netmesh_tsnet", lambda: True)
    added = []

    def add_forward(port, **kwargs):
        if add_error is not None:
            raise tunneld.TunneldForwardError(add_error)
        added.append((port, kwargs))
        return {"port": port}

    monkeypatch.setattr(tunneld, "tunneld_add_forward", add_forward)
    monkeypatch.setattr(
        tunneld,
        "tunneld_status",
        lambda: (True, {"Self": {"TailscaleIPs": [ipv4, "fd7a:115c:a1e0::12"]}}, None),
    )
    monkeypatch.setattr(
        tunneld, "tunneld_self_dns_name", lambda: "devspace-1.net.obs.so"
    )
    return added


def test_browser_open_reports_delivery(monkeypatch):
    calls = _patch_publish(monkeypatch, {"command_id": "c-1", "delivered": True})

    result = CliRunner().invoke(browser_cli.browser, ["open", PLAIN_URL])

    assert result.exit_code == 0
    assert result.output.splitlines() == [PLAIN_URL, browser_cli.OPENED_MESSAGE]
    assert calls == [PLAIN_URL]


def test_browser_open_prints_paste_back_hint_when_not_delivered(monkeypatch):
    _patch_publish(monkeypatch, {"command_id": "c-1", "delivered": False})

    result = CliRunner().invoke(browser_cli.browser, ["open", PLAIN_URL])

    assert result.exit_code == 0
    assert result.output.splitlines() == [PLAIN_URL, browser_cli.NOT_DELIVERED_HINT]
    assert "paste that final address back here" in result.output


def test_browser_open_forwards_the_login_callback_on_an_embedded_node(monkeypatch):
    added = _patch_tunneld(monkeypatch)
    calls = _patch_publish(monkeypatch, {"command_id": "c-1", "delivered": True})

    result = CliRunner().invoke(browser_cli.browser, ["open", LOGIN_URL])

    assert result.exit_code == 0, result.output
    assert added == [(1455, {"ttl_seconds": 600, "one_shot": True})]
    ((url, forward),) = calls
    assert url == LOGIN_URL
    assert forward["port"] == 1455
    assert forward["target"] == "100.64.0.12"
    assert forward["ttl_seconds"] == 600
    assert len(forward["token"]) >= 16
    assert "Workspace callback localhost:1455 is exposed" in result.output
    assert "If your phone cannot forward it" in result.output


def test_browser_open_explicit_callback_port_and_no_forward(monkeypatch):
    added = _patch_tunneld(monkeypatch)
    calls = _patch_publish(monkeypatch, {"command_id": "c-1", "delivered": True})
    plain = "https://auth.example.com/device"

    CliRunner().invoke(browser_cli.browser, ["open", plain, "--callback-port", "8085"])
    assert added == [(8085, {"ttl_seconds": 600, "one_shot": True})]
    assert calls[-1][1]["port"] == 8085

    CliRunner().invoke(browser_cli.browser, ["open", LOGIN_URL, "--no-forward"])
    assert len(added) == 1
    assert calls[-1] == LOGIN_URL


def test_browser_open_without_embedded_node_explains_paste_back(monkeypatch):
    calls = _patch_publish(monkeypatch, {"command_id": "c-1", "delivered": True})

    result = CliRunner().invoke(browser_cli.browser, ["open", LOGIN_URL])

    assert result.exit_code == 0
    assert "Could not expose localhost:1455" in result.output
    assert calls == [LOGIN_URL]


def test_browser_open_survives_a_refused_forward(monkeypatch):
    _patch_tunneld(monkeypatch, add_error="port 1455 is already forwarded")
    calls = _patch_publish(monkeypatch, {"command_id": "c-1", "delivered": True})

    result = CliRunner().invoke(browser_cli.browser, ["open", LOGIN_URL])

    assert result.exit_code == 0
    assert "Could not expose localhost:1455" in result.output
    assert calls == [LOGIN_URL]


def test_browser_open_falls_back_to_a_cloud_push(monkeypatch):
    _patch_tunneld(monkeypatch)
    _patch_publish(monkeypatch, {"command_id": "c-1", "delivered": False})
    pushes = []
    monkeypatch.setattr(
        browser_cli, "_push", lambda url, forward: pushes.append((url, forward)) or True
    )

    result = CliRunner().invoke(browser_cli.browser, ["open", LOGIN_URL])

    assert result.exit_code == 0
    assert browser_cli.PUSHED_MESSAGE in result.output
    ((url, forward),) = pushes
    assert url == LOGIN_URL and forward.port == 1455

    pushes.clear()
    result = CliRunner().invoke(browser_cli.browser, ["open", LOGIN_URL, "--no-push"])
    assert pushes == []
    assert browser_cli.NOT_DELIVERED_HINT in result.output


def test_push_sends_open_url_user_info(monkeypatch):
    from openbase_coder_cli.config import cloud_notifications
    from openbase_coder_cli.login_callback import LoopbackForward

    sent = []
    monkeypatch.setattr(
        cloud_notifications,
        "send_notification_push",
        lambda **kwargs: sent.append(kwargs),
    )
    forward = LoopbackForward(
        port=1455, target="100.64.0.12", ttl_seconds=600, token="t" * 24
    )

    assert browser_cli._push(LOGIN_URL, forward) is True
    assert sent[0]["title"] == browser_cli.PUSH_TITLE
    assert sent[0]["body"] == "Tap to open auth.example.com"
    assert sent[0]["user_info"] == {
        "openbase_destination": "open_url",
        "url": LOGIN_URL,
        "forward_port": "1455",
        "forward_target": "100.64.0.12",
        "forward_ttl_seconds": "600",
        "forward_token": "t" * 24,
    }

    def boom(**kwargs):
        raise RuntimeError("cloud down")

    monkeypatch.setattr(cloud_notifications, "send_notification_push", boom)
    assert browser_cli._push(LOGIN_URL, None) is False


def test_browser_open_succeeds_when_local_server_is_unreachable(monkeypatch):
    def refuse(method, url, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(local_server, "get_local_api_token", lambda: "local-token")
    monkeypatch.setattr(local_server.httpx, "request", refuse)

    result = CliRunner().invoke(browser_cli.browser, ["open", PLAIN_URL])

    assert result.exit_code == 0
    assert result.output.splitlines() == [PLAIN_URL, browser_cli.NOT_DELIVERED_HINT]


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

    for url in (
        "javascript:alert(1)",
        "file:///etc/passwd",
        "no-scheme",
        "https://[invalid",
    ):
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


def test_browser_open_help_documents_forwarding_options():
    result = CliRunner().invoke(browser_cli.browser, ["open", "--help"])

    assert result.exit_code == 0
    assert "--no-forward" in result.output
    assert "--callback-port" in result.output
    assert "--no-push" in result.output


def test_browser_open_bounds_stalled_delivery(monkeypatch):
    release = threading.Event()

    def stalled(url, *, loopback_forward=None):
        release.wait(5)
        return {"delivered": True}

    monkeypatch.setattr(browser_cli, "publish_open_url", stalled)
    monkeypatch.setattr(browser_cli, "BROWSER_DELIVERY_TIMEOUT_SECONDS", 0.02)
    started = time.monotonic()
    try:
        result = CliRunner().invoke(browser_cli.browser, ["open", LOGIN_URL])
        assert time.monotonic() - started < 1
        assert result.exit_code == 0
        assert browser_cli.NOT_DELIVERED_HINT in result.output
    finally:
        release.set()


def test_container_browser_shim_works_with_python_webbrowser(tmp_path):
    shim = tmp_path / "openbase-browser"
    shim.write_bytes(
        (Path(__file__).parents[1] / "docker/openbase-browser").read_bytes()
    )
    shim.chmod(0o755)
    capture = tmp_path / "arguments.json"
    executable = tmp_path / "openbase-coder"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['BROWSER_TEST_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "BROWSER": "openbase-browser",
        "BROWSER_TEST_CAPTURE": str(capture),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, webbrowser; sys.exit(not webbrowser.open(sys.argv[1]))",
            LOGIN_URL,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text()) == ["browser", "open", LOGIN_URL]


@pytest.mark.parametrize("notified", [True, False])
def test_failed_open_only_suppresses_push_when_a_notification_was_posted(
    monkeypatch, notified
):
    _patch_publish(
        monkeypatch, {"delivered": True, "opened": False, "notified": notified}
    )
    pushes = []
    monkeypatch.setattr(browser_cli, "_push", lambda *args: pushes.append(args) or True)
    result = CliRunner().invoke(browser_cli.browser, ["open", PLAIN_URL])
    assert result.exit_code == 0
    assert len(pushes) == (0 if notified else 1)


@pytest.mark.parametrize("stage", ["forward", "push"])
def test_browser_bounds_stalled_forward_and_push(monkeypatch, stage):
    from openbase_coder_cli.config import cloud_notifications
    from openbase_coder_cli.services import tunneld

    release = threading.Event()
    finished = threading.Event()

    def stalled(*args, **kwargs):
        release.wait(5)
        finished.set()
        raise OSError("unavailable")

    if stage == "forward":
        _patch_tunneld(monkeypatch)
        monkeypatch.setattr(tunneld, "tunneld_status", stalled)
    else:
        monkeypatch.setattr(cloud_notifications, "send_notification_push", stalled)
    _patch_publish(monkeypatch, {"delivered": False})
    monkeypatch.setattr(browser_cli, "BROWSER_DELIVERY_TIMEOUT_SECONDS", 0.02)
    started = time.monotonic()
    try:
        result = CliRunner().invoke(browser_cli.browser, ["open", LOGIN_URL])
        assert time.monotonic() - started < 1
        assert result.exit_code == 0
        assert browser_cli.NOT_DELIVERED_HINT in result.output
    finally:
        release.set()
        assert finished.wait(1)


def test_forward_setup_exception_does_not_fail_browser_handler(monkeypatch):
    from openbase_coder_cli.services import tunneld

    _patch_tunneld(monkeypatch)

    def unavailable():
        raise OSError("control token unavailable")

    monkeypatch.setattr(tunneld, "tunneld_status", unavailable)
    _patch_publish(monkeypatch, {"delivered": True})
    result = CliRunner().invoke(browser_cli.browser, ["open", LOGIN_URL])
    assert result.exit_code == 0
    assert browser_cli.OPENED_MESSAGE in result.output


def test_forward_target_never_falls_back_to_dns_or_a_non_vpn_address():
    assert (
        browser_cli._self_tailnet_target(
            lambda: (
                True,
                {"Self": {"TailscaleIPs": ["192.168.1.2"], "DNSName": "evil.example"}},
                None,
            )
        )
        is None
    )
    assert (
        browser_cli._self_tailnet_target(
            lambda: (True, {"Self": {"TailscaleIPs": ["fd7a:115c:a1e0::12"]}}, None)
        )
        == "fd7a:115c:a1e0::12"
    )
