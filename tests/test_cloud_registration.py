from __future__ import annotations

import json

import httpx
import pytest

from openbase_coder_cli.config.token_manager import AuthLoginRequiredError
from openbase_coder_cli.services import cloud_registration, onboarding


@pytest.fixture
def onboarding_cache(monkeypatch, tmp_path):
    cache_path = tmp_path / "onboarding.json"
    monkeypatch.setattr(onboarding, "ONBOARDING_JSON_PATH", cache_path)
    return cache_path


@pytest.fixture
def logged_in(monkeypatch):
    class FakeTokenManager:
        def __init__(self, web_backend_url):
            pass

        def get_access_token(self):
            return "jwt.token"

    monkeypatch.setattr(cloud_registration, "TokenManager", FakeTokenManager)


def _mock_response(monkeypatch, response: httpx.Response):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return response

    monkeypatch.setattr(cloud_registration.httpx, "request", fake_request)
    return calls


def _fake_identity(available: bool = True) -> dict:
    return {
        "available": available,
        "tailscale_available": True,
        "dns_name": "mac.tailnet.ts.net" if available else None,
        "node_hostname": "mac",
        "tailnet": "tailnet.ts.net",
        "ips": ["100.64.0.1"] if available else [],
        "error": None,
    }


def test_device_registration_payload_includes_tailscale(monkeypatch) -> None:
    monkeypatch.setattr(
        cloud_registration, "tailscale_self_identity", _fake_identity
    )
    monkeypatch.setattr(cloud_registration, "_device_id", lambda: "desktop-1")

    payload = cloud_registration.device_registration_payload()

    assert payload["device_id"] == "desktop-1"
    assert payload["kind"] == "desktop"
    assert payload["hostname"]
    assert payload["platform"] in ("darwin", "linux")
    assert payload["tailscale"] == {
        "dns_name": "mac.tailnet.ts.net",
        "node_hostname": "mac",
        "tailnet": "tailnet.ts.net",
        "ips": ["100.64.0.1"],
    }
    assert payload["tailscale_ip"] == "100.64.0.1"
    assert payload["tailscale_magic_dns"] == "mac.tailnet.ts.net"


def test_device_registration_payload_omits_tailscale_when_down(monkeypatch) -> None:
    monkeypatch.setattr(
        cloud_registration,
        "tailscale_self_identity",
        lambda: _fake_identity(available=False),
    )

    payload = cloud_registration.device_registration_payload()

    assert "tailscale" not in payload
    assert "tailscale_ip" not in payload
    assert "tailscale_magic_dns" not in payload


def test_device_registration_payload_reuses_cached_device_id(
    monkeypatch, onboarding_cache
) -> None:
    monkeypatch.setattr(
        cloud_registration,
        "tailscale_self_identity",
        lambda: _fake_identity(available=False),
    )

    first = cloud_registration.device_registration_payload()
    second = cloud_registration.device_registration_payload()

    assert first["device_id"].startswith("desktop-")
    assert second["device_id"] == first["device_id"]
    cache = json.loads(onboarding_cache.read_text(encoding="utf-8"))
    assert cache["device_id"] == first["device_id"]


def test_register_success_writes_cache(
    monkeypatch, onboarding_cache, logged_in
) -> None:
    monkeypatch.setattr(
        cloud_registration, "tailscale_self_identity", _fake_identity
    )
    calls = _mock_response(monkeypatch, httpx.Response(201, json={}))

    result = cloud_registration.register_device_with_cloud()

    assert result.ok is True
    assert result.supported is True
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/api/openbase/devices/register/")
    assert calls[0]["headers"]["Authorization"] == "Bearer jwt.token"

    cache = json.loads(onboarding_cache.read_text(encoding="utf-8"))
    assert cache["last_register"]["ok"] is True


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"detail": "Not found."}),
        httpx.Response(405, json={"detail": "Method not allowed."}),
        httpx.Response(
            403,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html>403 Forbidden</html>",
        ),
    ],
)
def test_unshipped_endpoint_reports_unsupported(
    monkeypatch, onboarding_cache, logged_in, response
) -> None:
    monkeypatch.setattr(
        cloud_registration, "tailscale_self_identity", _fake_identity
    )
    _mock_response(monkeypatch, response)

    result = cloud_registration.register_device_with_cloud()

    assert result.ok is False
    assert result.supported is False


def test_json_error_reports_supported_failure(
    monkeypatch, onboarding_cache, logged_in
) -> None:
    monkeypatch.setattr(
        cloud_registration, "tailscale_self_identity", _fake_identity
    )
    _mock_response(
        monkeypatch,
        httpx.Response(
            400,
            headers={"content-type": "application/json"},
            json={"detail": "bad payload"},
        ),
    )

    result = cloud_registration.register_device_with_cloud()

    assert result.ok is False
    assert result.supported is True
    assert "400" in (result.error or "")


def test_login_required_never_raises(monkeypatch, onboarding_cache) -> None:
    class FakeTokenManager:
        def __init__(self, web_backend_url):
            pass

        def get_access_token(self):
            raise AuthLoginRequiredError("missing")

    monkeypatch.setattr(cloud_registration, "TokenManager", FakeTokenManager)
    monkeypatch.setattr(
        cloud_registration, "tailscale_self_identity", _fake_identity
    )

    result = cloud_registration.register_device_with_cloud()

    assert result.ok is False
    assert result.supported is True
    assert "login" in (result.error or "").lower()


def test_register_and_report_returns_registration_failure(
    monkeypatch, onboarding_cache, logged_in
) -> None:
    monkeypatch.setattr(
        cloud_registration, "tailscale_self_identity", _fake_identity
    )
    calls = _mock_response(
        monkeypatch, httpx.Response(404, json={"detail": "Not found."})
    )

    result = cloud_registration.register_and_report(
        cli_configured=True, serve_healthy=True
    )

    assert result.supported is False
    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/api/openbase/devices/register/")


def test_report_cli_state_posts_registration_capabilities(
    monkeypatch, onboarding_cache, logged_in
) -> None:
    monkeypatch.setattr(
        cloud_registration, "tailscale_self_identity", _fake_identity
    )
    calls = _mock_response(monkeypatch, httpx.Response(200, json={}))

    result = cloud_registration.report_cli_state(
        cli_configured=True, serve_healthy=False
    )

    assert result.ok is True
    assert calls[0]["url"].endswith("/api/openbase/devices/register/")
    assert calls[0]["method"] == "POST"
    assert calls[0]["json"]["capabilities"]["cli_configured"] is True
    assert calls[0]["json"]["capabilities"]["tailscale_serve_healthy"] is False

    cache = json.loads(onboarding_cache.read_text(encoding="utf-8"))
    assert cache["last_report"]["cli_configured"] is True


def test_service_hostname_cloud_contract_uses_authenticated_bounded_endpoints(
    monkeypatch, logged_in
) -> None:
    calls = _mock_response(
        monkeypatch,
        httpx.Response(
            200,
            json={
                "supported": True,
                "dns_allocation": True,
                "serve_routing": False,
                "pattern": "{service}.{node_dns_name}",
                "http_port": 80,
            },
        ),
    )

    result = cloud_registration.netmesh_service_hostname_capabilities()

    assert result.ok is True
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["url"].endswith(
        "/api/openbase/netmesh/service-hostnames/capabilities/"
    )

    allocation_calls = _mock_response(
        monkeypatch, httpx.Response(200, json={"hostname": "x"})
    )
    result = cloud_registration.allocate_netmesh_service_hostname(
        node_id="7", service_name="crm"
    )
    assert result.ok is True
    assert allocation_calls[0]["method"] == "POST"
    assert allocation_calls[0]["json"] == {
        "node_id": "7",
        "service_name": "crm",
    }
    # The transport itself is covered above; verify release separately through
    # a fresh call recorder so DELETE cannot regress to an unauthenticated path.
    release_calls = _mock_response(monkeypatch, httpx.Response(204))
    result = cloud_registration.release_netmesh_service_hostname(
        node_id="7", service_name="crm"
    )
    assert result.ok is True
    assert release_calls[0]["method"] == "DELETE"
    assert release_calls[0]["json"] == {"node_id": "7", "service_name": "crm"}
    assert release_calls[0]["headers"]["Authorization"] == "Bearer jwt.token"
