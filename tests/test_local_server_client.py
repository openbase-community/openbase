import httpx
import pytest

from openbase_coder_cli.cli import local_server


@pytest.mark.parametrize("url", ["http://127.0.0.1:7999", "http://[::1]:7999", "http://localhost:7999"])
def test_loopback_request_does_not_require_cloud_login(monkeypatch, url):
    monkeypatch.setenv("OPENBASE_CODER_CLI_SERVER_URL", url)
    monkeypatch.setattr(local_server, "get_local_api_token", lambda: "installation-capability")
    def cloud_unavailable():
        pytest.fail("A loopback operation must not contact cloud authentication")
    monkeypatch.setattr(local_server, "get_token_manager", cloud_unavailable)
    def request(method, destination, **kwargs):
        assert kwargs['follow_redirects'] is False
        message = httpx.Request(method, destination)
        authenticated = next(kwargs['auth'].auth_flow(message))
        assert authenticated.headers['Authorization'] == 'Bearer installation-capability'
        return httpx.Response(202)
    monkeypatch.setattr(local_server.httpx, "request", request)
    assert local_server.local_server_request("POST", "/api/user/say/", follow_redirects=True).status_code == 202


@pytest.mark.parametrize("url", ["https://remote.example", "http://localhost.example:7999", "http://127.0.0.1.example:7999"])
def test_remote_override_never_receives_local_capability(monkeypatch, url):
    class Manager:
        def get_access_token(self):
            return 'cloud.jwt.token'
    monkeypatch.setattr(local_server, "get_token_manager", lambda: Manager())
    def forbidden():
        pytest.fail("Remote authentication must not read the local capability")
    monkeypatch.setattr(local_server, "get_local_api_token", forbidden)
    request = next(local_server.server_auth(url).auth_flow(httpx.Request('POST', url)))
    assert request.headers['Authorization'] == 'Bearer cloud.jwt.token'
