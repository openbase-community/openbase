from __future__ import annotations

import importlib

import httpx
from click.testing import CliRunner

from openbase_coder_cli.config.token_manager import AuthLoginRequiredError

cloud_cli = importlib.import_module("openbase_coder_cli.cli.cloud")


class FakeTokenManager:
    stored_tokens = []

    def __init__(self, web_backend_url: str | None = None):
        self.web_backend_url = web_backend_url

    def load(self) -> None:
        pass

    def get_access_token(self) -> str:
        return "jwt.token.value"

    def store_tokens(self, **kwargs) -> None:
        self.stored_tokens.append((self.web_backend_url, kwargs))


class LoginRequiredTokenManager(FakeTokenManager):
    def get_access_token(self) -> str:
        raise AuthLoginRequiredError("login required")


def _patch_activity_response(monkeypatch, handler) -> None:
    def fake_get(url, **kwargs):
        return handler(url, **kwargs)

    monkeypatch.setattr(cloud_cli.httpx, "get", fake_get)


def test_agent_runs_active_true_when_runs_reported(monkeypatch):
    _patch_activity_response(
        monkeypatch,
        lambda url, **kwargs: httpx.Response(
            200, json={"active_run_count": 2, "thread_count": 5}
        ),
    )

    assert cloud_cli._agent_runs_active("http://127.0.0.1:7999", FakeTokenManager())


def test_agent_runs_active_false_when_no_runs(monkeypatch):
    _patch_activity_response(
        monkeypatch,
        lambda url, **kwargs: httpx.Response(
            200, json={"active_run_count": 0, "thread_count": 5}
        ),
    )

    assert not cloud_cli._agent_runs_active("http://127.0.0.1:7999", FakeTokenManager())


def test_agent_runs_active_false_when_server_unreachable(monkeypatch):
    def raise_connect_error(url, **kwargs):
        raise httpx.ConnectError("connection refused")

    _patch_activity_response(monkeypatch, raise_connect_error)

    assert not cloud_cli._agent_runs_active("http://127.0.0.1:7999", FakeTokenManager())


def test_agent_runs_active_false_on_error_status(monkeypatch):
    _patch_activity_response(
        monkeypatch,
        lambda url, **kwargs: httpx.Response(401, json={"detail": "unauthorized"}),
    )

    assert not cloud_cli._agent_runs_active("http://127.0.0.1:7999", FakeTokenManager())


def test_single_heartbeat_posts_run_activity(monkeypatch):
    posts = []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs["json"]))
        return httpx.Response(200, json={"message": "Heartbeat recorded."})

    monkeypatch.setattr(cloud_cli, "TokenManager", FakeTokenManager)
    monkeypatch.setattr(cloud_cli, "_web_backend_url", lambda: "https://cloud.test")
    monkeypatch.setattr(
        cloud_cli, "_agent_runs_active", lambda local_url, manager: True
    )
    monkeypatch.setattr(cloud_cli.httpx, "post", fake_post)

    result = CliRunner().invoke(cloud_cli.cloud, ["heartbeat", "--interval", "0"])

    assert result.exit_code == 0
    assert posts == [
        ("https://cloud.test/api/openbase/devspaces/heartbeat/", {"active": True})
    ]


def test_single_heartbeat_reports_inactive_without_runs(monkeypatch):
    posts = []

    def fake_post(url, **kwargs):
        posts.append(kwargs["json"])
        return httpx.Response(200, json={"message": "Heartbeat recorded."})

    monkeypatch.setattr(cloud_cli, "TokenManager", FakeTokenManager)
    monkeypatch.setattr(cloud_cli, "_web_backend_url", lambda: "https://cloud.test")
    monkeypatch.setattr(
        cloud_cli, "_agent_runs_active", lambda local_url, manager: False
    )
    monkeypatch.setattr(cloud_cli.httpx, "post", fake_post)

    result = CliRunner().invoke(cloud_cli.cloud, ["heartbeat", "--interval", "0"])

    assert result.exit_code == 0
    assert posts == [{"active": False}]


def test_rehydrate_auth_stores_tokens_and_registers(monkeypatch, tmp_path):
    posts = []
    FakeTokenManager.stored_tokens = []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs["json"]))
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "web_backend_url": "https://cloud.test",
            },
        )

    class FakeMachineTokenManager:
        def __init__(self, url, manager):
            self.url = url
            self.manager = manager

        def get_machine_token(self, *, rotate=False):
            assert rotate is True
            return "obmt_token"

    monkeypatch.setattr(cloud_cli, "TokenManager", LoginRequiredTokenManager)
    monkeypatch.setattr(cloud_cli, "_web_backend_url", lambda: "https://cloud.test")
    monkeypatch.setattr(
        cloud_cli,
        "build_instance_rehydrate_payload",
        lambda: {"workspace_id": "abc123"},
    )
    monkeypatch.setattr(cloud_cli.httpx, "post", fake_post)
    monkeypatch.setattr(cloud_cli, "MachineTokenManager", FakeMachineTokenManager)
    monkeypatch.setattr(
        cloud_cli,
        "register_and_report",
        lambda: type("Result", (), {"ok": True, "supported": True, "error": None})(),
    )
    monkeypatch.setattr(cloud_cli, "DEFAULT_ENV_FILE_PATH", tmp_path / ".env")

    result = CliRunner().invoke(cloud_cli.cloud, ["rehydrate-auth"])

    assert result.exit_code == 0
    assert posts == [
        (
            "https://cloud.test/api/openbase/devspaces/instance-auth/rehydrate/",
            {"workspace_id": "abc123"},
        )
    ]
    assert FakeTokenManager.stored_tokens == [
        (
            "https://cloud.test",
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 300,
            },
        )
    ]
    assert "Cloud auth rehydrated." in result.output


def test_agent_runs_active_honours_combined_active_flag(monkeypatch):
    _patch_activity_response(
        monkeypatch,
        lambda url, **kwargs: httpx.Response(
            200,
            json={"active_run_count": 0, "active_call_count": 1, "active": True},
        ),
    )
    assert cloud_cli._agent_runs_active("http://127.0.0.1:7999", FakeTokenManager())


def test_agent_runs_active_uses_local_api_token_without_login(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        request = httpx.Request("GET", url)
        flow = kwargs["auth"].auth_flow(request)
        request = next(flow)
        seen["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"active": True})

    monkeypatch.setattr(cloud_cli.httpx, "get", fake_get)
    monkeypatch.setattr(
        "openbase_coder_cli.config.local_api_token.get_local_api_token",
        lambda: "local-capability-token",
    )
    assert cloud_cli._agent_runs_active(
        "http://127.0.0.1:7999", LoginRequiredTokenManager()
    )
    assert seen["authorization"] == "Bearer local-capability-token"


def test_cloud_bearer_token_falls_back_to_machine_token(monkeypatch):
    class FakeMachineTokens:
        def __init__(self, url):
            self.url = url

        def has_cached_token(self):
            return True

        def get_machine_token(self):
            return "machine.token"

    monkeypatch.setattr(
        "openbase_coder_cli.config.machine_token_manager.MachineTokenManager",
        FakeMachineTokens,
    )
    seen = {}

    class RecordingMachineTokens(FakeMachineTokens):
        def __init__(self, url):
            seen["url"] = url

    monkeypatch.setattr(
        "openbase_coder_cli.config.machine_token_manager.MachineTokenManager",
        RecordingMachineTokens,
    )
    # The real TokenManager keeps its URL private; the caller supplies it.
    from openbase_coder_cli.config.token_manager import TokenManager

    manager = TokenManager(web_backend_url="https://cloud.example")
    assert (
        cloud_cli._cloud_bearer_token(manager, "https://cloud.example")
        == "machine.token"
    )
    assert seen["url"] == "https://cloud.example"
