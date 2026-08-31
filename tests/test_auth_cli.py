from __future__ import annotations

import importlib
import json
import time

from click.testing import CliRunner

from openbase_coder_cli.cli import main
from openbase_coder_cli.config.token_manager import AuthLoginRequiredError

auth_cli = importlib.import_module("openbase_coder_cli.cli.auth")


def test_auth_print_access_token_outputs_token(monkeypatch):
    class FakeTokenManager:
        def __init__(self, web_backend_url):
            self.web_backend_url = web_backend_url

        def get_access_token(self):
            return "jwt.token.value"

    monkeypatch.setattr(auth_cli, "TokenManager", FakeTokenManager)
    monkeypatch.setenv("OPENBASE_CODER_CLI_WEB_BACKEND_URL", "https://backend.example")

    result = CliRunner().invoke(main, ["auth", "print-access-token"])

    assert result.exit_code == 0
    assert result.output == "jwt.token.value\n"


def test_auth_print_access_token_reports_login_required(monkeypatch):
    class FakeTokenManager:
        def __init__(self, web_backend_url):
            pass

        def get_access_token(self):
            raise AuthLoginRequiredError("missing")

    monkeypatch.setattr(auth_cli, "TokenManager", FakeTokenManager)

    result = CliRunner().invoke(main, ["auth", "print-access-token"])

    assert result.exit_code != 0
    assert "openbase-coder login" in result.output


def test_auth_print_machine_token_outputs_token(monkeypatch):
    class FakeMachineTokenManager:
        def __init__(self, web_backend_url):
            self.web_backend_url = web_backend_url

        def get_machine_token(self, *, rotate=False):
            assert rotate is False
            return "obmt_machine_token"

    monkeypatch.setattr(auth_cli, "MachineTokenManager", FakeMachineTokenManager)
    monkeypatch.setenv("OPENBASE_CODER_CLI_WEB_BACKEND_URL", "https://backend.example")

    result = CliRunner().invoke(main, ["auth", "print-machine-token"])

    assert result.exit_code == 0
    assert result.output == "obmt_machine_token\n"


def test_auth_print_machine_token_can_rotate(monkeypatch):
    class FakeMachineTokenManager:
        def __init__(self, web_backend_url):
            pass

        def get_machine_token(self, *, rotate=False):
            assert rotate is True
            return "obmt_rotated"

    monkeypatch.setattr(auth_cli, "MachineTokenManager", FakeMachineTokenManager)

    result = CliRunner().invoke(main, ["auth", "print-machine-token", "--rotate"])

    assert result.exit_code == 0
    assert result.output == "obmt_rotated\n"


def _fake_status_token_manager(status, *, validated=True, email="user@example.com"):
    class FakeTokenManager:
        def __init__(self, web_backend_url):
            pass

        def login_status(self):
            return {"status": status, "validated": validated, "detail": ""}

        def get_owner_identity(self):
            return {"email": email} if email else {}

    return FakeTokenManager


def test_auth_status_json_always_exits_zero(monkeypatch):
    monkeypatch.setattr(
        auth_cli, "TokenManager", _fake_status_token_manager("login_expired")
    )

    result = CliRunner().invoke(main, ["auth", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "login_expired"
    assert payload["validated"] is True
    assert payload["email"] == "user@example.com"


def test_auth_status_reports_logged_in_with_email(monkeypatch):
    monkeypatch.setattr(
        auth_cli, "TokenManager", _fake_status_token_manager("logged_in")
    )

    result = CliRunner().invoke(main, ["auth", "status"])

    assert result.exit_code == 0
    assert "Logged in as user@example.com" in result.output


def test_auth_status_expired_exits_nonzero(monkeypatch):
    monkeypatch.setattr(
        auth_cli, "TokenManager", _fake_status_token_manager("login_expired")
    )

    result = CliRunner().invoke(main, ["auth", "status"])

    assert result.exit_code != 0
    assert "openbase-coder login" in result.output


def test_auth_status_logged_out_exits_nonzero(monkeypatch):
    monkeypatch.setattr(
        auth_cli, "TokenManager", _fake_status_token_manager("logged_out")
    )

    result = CliRunner().invoke(main, ["auth", "status"])

    assert result.exit_code != 0
    assert "Not logged in" in result.output


def test_oauth_success_page_announces_success_and_returns_to_desktop():
    html = auth_cli._oauth_success_html().decode("utf-8")

    assert "Logged in successfully" in html
    assert "Open the Mac app" in html
    assert "openbase-coder://open?source=cli-auth&amp;intent=login-complete" in html
    assert '"openbase-coder://open?source=cli-auth&intent=login-complete"' in html
    assert "window.location.href" in html


def _free_port():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_wait_for_callback_gives_up_and_releases_the_port(monkeypatch):
    """An abandoned browser flow must not pin the callback port forever.

    The listener used to loop with no deadline, so a login the user never
    completed kept 127.0.0.1:52807 bound for the life of the process. Every
    later login then failed to bind and silently degraded to manual paste.
    """
    import socket

    port = _free_port()
    monkeypatch.setattr(
        auth_cli, "_prompt_for_pasted_callback", lambda state: {"code": "pasted"}
    )

    result = auth_cli._wait_for_callback(
        f"http://127.0.0.1:{port}/oauth/callback",
        expected_state="state-123",
        timeout_seconds=0.1,
    )

    assert result == {"code": "pasted"}

    # The port is bindable again only if the listener was actually closed.
    with socket.socket() as after:
        after.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        after.bind(("127.0.0.1", port))


def test_wait_for_callback_still_accepts_a_real_redirect():
    """The timeout must not break the normal success path."""
    import threading
    import urllib.request

    port = _free_port()
    captured = {}

    def run():
        captured["result"] = auth_cli._wait_for_callback(
            f"http://127.0.0.1:{port}/oauth/callback",
            expected_state="state-123",
            timeout_seconds=30,
        )

    listener = threading.Thread(target=run, daemon=True)
    listener.start()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/oauth/callback?code=the-code&state=state-123",
                timeout=5,
            ).read()
            break
        except OSError:
            time.sleep(0.05)

    listener.join(timeout=15)
    assert captured["result"]["code"] == "the-code"
