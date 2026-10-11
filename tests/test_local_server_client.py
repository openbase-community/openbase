import httpx
import pytest

from openbase_coder_cli.cli import local_server


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:7999", "http://[::1]:7999", "http://localhost:7999"]
)
def test_loopback_request_does_not_require_cloud_login(monkeypatch, url):
    monkeypatch.setenv("OPENBASE_CODER_CLI_SERVER_URL", url)
    monkeypatch.setattr(
        local_server, "get_local_api_token", lambda: "installation-capability"
    )

    def cloud_unavailable():
        pytest.fail("A loopback operation must not contact cloud authentication")

    monkeypatch.setattr(local_server, "get_token_manager", cloud_unavailable)

    def request(method, destination, **kwargs):
        assert kwargs["follow_redirects"] is False
        message = httpx.Request(method, destination)
        authenticated = next(kwargs["auth"].auth_flow(message))
        assert (
            authenticated.headers["Authorization"] == "Bearer installation-capability"
        )
        return httpx.Response(202)

    monkeypatch.setattr(local_server.httpx, "request", request)
    assert (
        local_server.local_server_request(
            "POST", "/api/user/say/", follow_redirects=True
        ).status_code
        == 202
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://remote.example",
        "http://localhost.example:7999",
        "http://127.0.0.1.example:7999",
    ],
)
def test_remote_override_never_receives_local_capability(monkeypatch, url):
    class Manager:
        def get_access_token(self):
            return "cloud.jwt.token"

    monkeypatch.setattr(local_server, "get_token_manager", lambda: Manager())

    def forbidden():
        pytest.fail("Remote authentication must not read the local capability")

    monkeypatch.setattr(local_server, "get_local_api_token", forbidden)
    request = next(local_server.server_auth(url).auth_flow(httpx.Request("POST", url)))
    assert request.headers["Authorization"] == "Bearer cloud.jwt.token"


def test_local_server_url_follows_the_configured_host_and_port(monkeypatch):
    """Container runtimes start the server off 7999 (Maritime: 18789); every
    local probe, including the Cloud heartbeat's call check, must follow."""
    monkeypatch.delenv("OPENBASE_CODER_CLI_SERVER_URL", raising=False)
    monkeypatch.delenv("OPENBASE_CODER_CLI_LOCAL_SERVER_URL", raising=False)
    monkeypatch.setenv("OPENBASE_CODER_CLI_HOST", "127.0.0.1")
    monkeypatch.setenv("OPENBASE_CODER_CLI_PORT", "18789")

    assert local_server.local_server_url() == "http://127.0.0.1:18789"


def test_local_server_url_defaults_and_explicit_url_wins(monkeypatch):
    for name in (
        "OPENBASE_CODER_CLI_SERVER_URL",
        "OPENBASE_CODER_CLI_LOCAL_SERVER_URL",
        "OPENBASE_CODER_CLI_HOST",
        "OPENBASE_CODER_CLI_PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    assert local_server.local_server_url() == "http://127.0.0.1:7999"

    monkeypatch.setenv("OPENBASE_CODER_CLI_PORT", "not-a-port")
    assert local_server.local_server_url() == "http://127.0.0.1:7999"

    monkeypatch.setenv("OPENBASE_CODER_CLI_PORT", "18789")
    monkeypatch.setenv("OPENBASE_CODER_CLI_SERVER_URL", "http://127.0.0.1:9000/")
    assert local_server.local_server_url() == "http://127.0.0.1:9000"
