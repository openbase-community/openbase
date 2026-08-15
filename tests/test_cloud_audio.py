from __future__ import annotations

from unittest import mock

import httpx
import pytest

from openbase_coder_cli.config import cloud_audio
from openbase_coder_cli.config.token_manager import AuthLoginRequiredError


class FakeTokenManager:
    def get_access_token(self) -> str:
        return "jwt.token.value"


def usage_response(status_code: int = 200, **payload) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request(
            "GET", "https://backend.example/api/openbase/audio/usage/"
        ),
    )


def user_profile_response(status_code: int = 200, **payload) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("GET", "https://backend.example/api/users/me/"),
    )


def test_openbase_cloud_audio_check_skips_direct_and_local_providers(monkeypatch):
    monkeypatch.setattr(
        cloud_audio,
        "get_token_manager",
        lambda web_backend_url: pytest.fail("cloud auth should not be used"),
    )

    cloud_audio.ensure_openbase_cloud_audio_subscription(
        tts_provider_id="cartesia",
        stt_provider_id="local_mlx_whisper",
        web_backend_url="https://backend.example",
    )


def test_openbase_cloud_audio_check_requires_active_subscription(monkeypatch):
    monkeypatch.setattr(
        cloud_audio,
        "get_token_manager",
        lambda web_backend_url: FakeTokenManager(),
    )
    response = usage_response(
        monthly_limit_cents=0,
        cartesia_remaining_cents=0,
        assemblyai_remaining_cents=0,
    )

    with mock.patch.object(httpx, "get", return_value=response):
        with pytest.raises(cloud_audio.OpenbaseCloudAudioSubscriptionError) as exc_info:
            cloud_audio.ensure_openbase_cloud_audio_subscription(
                tts_provider_id="openbase_cloud",
                stt_provider_id="openbase_cloud",
                web_backend_url="https://backend.example",
            )

    message = str(exc_info.value)
    assert "active Openbase subscription" in message
    assert "app.openbase.cloud" in message


def test_openbase_cloud_subscription_entitlement_allows_paid_feature(monkeypatch):
    monkeypatch.setattr(
        cloud_audio,
        "get_token_manager",
        lambda web_backend_url: FakeTokenManager(),
    )
    response = user_profile_response(active_subscription="pro")

    with mock.patch.object(httpx, "get", return_value=response):
        entitlement = cloud_audio.openbase_cloud_subscription_entitlement(
            web_backend_url="https://backend.example",
        )

    assert entitlement == {
        "has_active_subscription": True,
        "detail": "",
    }


def test_openbase_cloud_subscription_entitlement_locks_without_subscription(monkeypatch):
    monkeypatch.setattr(
        cloud_audio,
        "get_token_manager",
        lambda web_backend_url: FakeTokenManager(),
    )
    response = user_profile_response(active_subscription=None)

    with mock.patch.object(httpx, "get", return_value=response):
        entitlement = cloud_audio.openbase_cloud_subscription_entitlement(
            web_backend_url="https://backend.example",
        )

    assert entitlement["has_active_subscription"] is False
    assert "Apple Music playback requires" in entitlement["detail"]


def test_openbase_cloud_subscription_entitlement_can_use_request_token(monkeypatch):
    monkeypatch.setattr(
        cloud_audio,
        "get_token_manager",
        lambda web_backend_url: pytest.fail("request token should be used"),
    )
    response = user_profile_response(active_subscription="manual_lifetime_infinite_cap")

    with mock.patch.object(httpx, "get", return_value=response) as mocked_get:
        entitlement = cloud_audio.openbase_cloud_subscription_entitlement(
            web_backend_url="https://backend.example",
            access_token="caller.jwt.token",
        )

    assert entitlement["has_active_subscription"] is True
    assert mocked_get.call_args.kwargs["headers"]["Authorization"] == (
        "Bearer caller.jwt.token"
    )


def test_openbase_cloud_subscription_entitlement_falls_back_to_usage_cap(monkeypatch):
    monkeypatch.setattr(
        cloud_audio,
        "get_token_manager",
        lambda web_backend_url: FakeTokenManager(),
    )
    responses = [
        user_profile_response(email="legacy@example.com"),
        usage_response(monthly_limit_cents=100),
    ]

    with mock.patch.object(httpx, "get", side_effect=responses):
        entitlement = cloud_audio.openbase_cloud_subscription_entitlement(
            web_backend_url="https://backend.example",
        )

    assert entitlement["has_active_subscription"] is True


def test_openbase_cloud_audio_check_rejects_exhausted_selected_provider(monkeypatch):
    monkeypatch.setattr(
        cloud_audio,
        "get_token_manager",
        lambda web_backend_url: FakeTokenManager(),
    )
    response = usage_response(
        monthly_limit_cents=100,
        cartesia_remaining_cents=42,
        assemblyai_remaining_cents=0,
    )

    with mock.patch.object(httpx, "get", return_value=response):
        with pytest.raises(cloud_audio.OpenbaseCloudAudioSubscriptionError) as exc_info:
            cloud_audio.ensure_openbase_cloud_audio_subscription(
                tts_provider_id="cartesia",
                stt_provider_id="openbase_cloud",
                web_backend_url="https://backend.example",
            )

    message = str(exc_info.value)
    assert "AssemblyAI credits" in message
    assert "app.openbase.cloud" in message


def test_openbase_cloud_audio_check_preserves_backend_denial_detail(monkeypatch):
    monkeypatch.setattr(
        cloud_audio,
        "get_token_manager",
        lambda web_backend_url: FakeTokenManager(),
    )
    response = usage_response(403, detail="Subscribe to continue using cloud audio.")

    with mock.patch.object(httpx, "get", return_value=response):
        with pytest.raises(cloud_audio.OpenbaseCloudAudioSubscriptionError) as exc_info:
            cloud_audio.ensure_openbase_cloud_audio_subscription(
                tts_provider_id="openbase_cloud",
                stt_provider_id="assemblyai",
                web_backend_url="https://backend.example",
            )

    assert str(exc_info.value) == "Subscribe to continue using cloud audio."


def test_openbase_cloud_audio_check_treats_unauthorized_as_login_required(monkeypatch):
    monkeypatch.setattr(
        cloud_audio,
        "get_token_manager",
        lambda web_backend_url: FakeTokenManager(),
    )

    with mock.patch.object(httpx, "get", return_value=usage_response(401)):
        with pytest.raises(AuthLoginRequiredError):
            cloud_audio.ensure_openbase_cloud_audio_subscription(
                tts_provider_id="openbase_cloud",
                stt_provider_id="assemblyai",
                web_backend_url="https://backend.example",
            )
