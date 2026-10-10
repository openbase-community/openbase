"""Exercise authenticated routing and provider minting without paid requests."""

import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django
import httpx
import pytest
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

django.setup()

from openbase_coder_cli import dispatcher_config, paths  # noqa: E402
from openbase_coder_cli.config import authentication  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import dictation  # noqa: E402


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", tmp_path / "dispatcher.json"
    )
    monkeypatch.setattr(paths, "DEFAULT_ENV_FILE_PATH", tmp_path / "runtime.env")
    monkeypatch.delenv("ASSEMBLY_AI_API_KEY", raising=False)
    return paths.DEFAULT_ENV_FILE_PATH


def invoke(authenticated=True, body=None):
    request = APIRequestFactory().post(
        "/api/dictation/session/", body or {}, format="json"
    )
    if authenticated:
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return resolve("/api/dictation/session/").func(request)


def test_managed_selection_never_reads_or_mints_byok(config, monkeypatch):
    dispatcher_config.set_stt_provider("openbase_cloud")
    monkeypatch.setattr(
        dictation.httpx,
        "get",
        lambda *a, **k: pytest.fail("Managed must not mint BYOK"),
    )
    response = invoke(body={"provider": "assemblyai", "key": "caller-ignored"})
    assert response.status_code == 200
    assert response.data == {"provider": "openbase_cloud"}
    assert response["Cache-Control"] == "no-store"


@pytest.mark.parametrize("voice_model", ["gpt-live-1", "pipeline"])
def test_byok_uses_selected_backend_key_and_scoped_token(config, monkeypatch, voice_model):
    dispatcher_config.set_stt_provider("assemblyai")
    # Deliberately unrelated call engine: dictation still follows STT.
    dispatcher_config.set_voice_model(voice_model)
    config.write_text("ASSEMBLY_AI_API_KEY=backend-test-value\n")
    calls = []

    def mint(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"token": "temporary-test-capability"})

    monkeypatch.setattr(dictation.httpx, "get", mint)
    response = invoke(body={"provider": "openbase_cloud", "key": "caller-ignored"})
    assert response.status_code == 200
    assert response.data == {
        "provider": "assemblyai",
        "token": "temporary-test-capability",
    }
    assert "backend-test-value" not in str(response.data)
    url, options = calls[0]
    assert url == "https://streaming.assemblyai.com/v3/token"
    assert options["headers"] == {"Authorization": "backend-test-value"}
    assert options["params"] == {
        "expires_in_seconds": 60,
        "max_session_duration_seconds": 300,
    }
    assert options["follow_redirects"] is False
    assert response["Cache-Control"] == "no-store"


@pytest.mark.parametrize("provider", ["assemblyai", "deepgram", "local_mlx_whisper"])
def test_missing_key_or_unsupported_provider_never_falls_back(
    config, monkeypatch, provider
):
    dispatcher_config.set_stt_provider(provider)
    monkeypatch.setattr(
        dictation.httpx, "get", lambda *a, **k: pytest.fail("Must not call a provider")
    )
    response = invoke()
    assert response.status_code == 409
    assert response.data["code"] == (
        "missing_key" if provider == "assemblyai" else "unsupported_provider"
    )
    assert "token" not in response.data


def test_cleared_disk_key_overrides_stale_process_key(config, monkeypatch):
    dispatcher_config.set_stt_provider("assemblyai")
    monkeypatch.setenv("ASSEMBLY_AI_API_KEY", "stale-test-value")
    config.write_text("ASSEMBLY_AI_API_KEY=\n")
    assert invoke().data["code"] == "missing_key"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
def test_provider_errors_do_not_reset_openbase_auth_or_leak_details(
    config, monkeypatch, status
):
    dispatcher_config.set_stt_provider("assemblyai")
    config.write_text("ASSEMBLY_AI_API_KEY=backend-test-value\n")
    monkeypatch.setattr(
        dictation.httpx,
        "get",
        lambda *a, **k: httpx.Response(
            status, json={"error": "sensitive-provider-body"}
        ),
    )
    response = invoke()
    assert response.status_code == (422 if status in (401, 403) else 503)
    assert response.data["code"] == (
        "invalid_key" if status in (401, 403) else "provider_unavailable"
    )
    assert "sensitive-provider-body" not in str(response.data)
    assert "credits" not in response.data["detail"]


@pytest.mark.parametrize(
    "payload", [{}, {"token": ""}, {"token": 42}, {"token": "backend-test-value"}, []]
)
def test_malformed_provider_token_is_rejected(config, monkeypatch, payload):
    dispatcher_config.set_stt_provider("assemblyai")
    config.write_text("ASSEMBLY_AI_API_KEY=backend-test-value\n")
    monkeypatch.setattr(
        dictation.httpx, "get", lambda *a, **k: httpx.Response(200, json=payload)
    )
    assert invoke().status_code == 503


def test_provider_transport_failure_is_actionable(config, monkeypatch):
    dispatcher_config.set_stt_provider("assemblyai")
    config.write_text("ASSEMBLY_AI_API_KEY=backend-test-value\n")

    def fail(*a, **k):
        raise httpx.ConnectError("sensitive-request-context")

    monkeypatch.setattr(dictation.httpx, "get", fail)
    response = invoke()
    assert response.status_code == 503
    assert "sensitive-request-context" not in str(response.data)


def test_unauthenticated_cannot_mint(config, monkeypatch):
    monkeypatch.setattr(
        dictation.httpx, "get", lambda *a, **k: pytest.fail("Unauthenticated mint")
    )
    assert invoke(authenticated=False).status_code == 401


def test_another_cloud_identity_cannot_mint(config, monkeypatch):
    monkeypatch.setattr(
        authentication,
        "_get_validator",
        lambda: SimpleNamespace(validate=lambda token: {"sub": "other-user"}),
    )
    monkeypatch.setattr(
        authentication, "get_token_manager_owner", lambda: {"sub": "owner"}
    )
    monkeypatch.setattr(authentication, "is_owner_identity", lambda claims: False)
    monkeypatch.setattr(
        dictation.httpx, "get", lambda *a, **k: pytest.fail("Non-owner mint")
    )
    request = APIRequestFactory().post(
        "/api/dictation/session/",
        {},
        format="json",
        HTTP_AUTHORIZATION="Bearer header.payload.signature",
    )
    response = resolve("/api/dictation/session/").func(request)
    assert response.status_code == 401
    assert "not authorized" in str(response.data)
