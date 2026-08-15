"""Openbase Cloud audio proxy subscription checks."""

from __future__ import annotations

from collections.abc import Iterable

import httpx

from openbase_coder_cli.config.token_manager import (
    DEFAULT_WEB_BACKEND_URL,
    AuthLoginRequiredError,
    AuthTransientError,
    get_token_manager,
)
from openbase_coder_cli.stt_providers import OPENBASE_CLOUD_STT_PROVIDER_ID
from openbase_coder_cli.tts_providers import OPENBASE_CLOUD_TTS_PROVIDER_ID

OPENBASE_CLOUD_AUDIO_SUBSCRIBE_DETAIL = (
    "Openbase Cloud audio requires an active Openbase subscription with "
    "available audio credits. Subscribe at app.openbase.cloud, or switch "
    "voice settings to direct provider keys or local audio."
)
OPENBASE_CLOUD_SUBSCRIBE_DETAIL = (
    "Apple Music playback requires an active Openbase Cloud subscription."
)


class OpenbaseCloudAudioSubscriptionError(RuntimeError):
    """Openbase Cloud audio is selected, but the account cannot use it."""


def ensure_openbase_cloud_audio_subscription(
    *,
    tts_provider_id: str,
    stt_provider_id: str,
    web_backend_url: str = DEFAULT_WEB_BACKEND_URL,
) -> None:
    providers = _required_cloud_audio_providers(
        tts_provider_id=tts_provider_id,
        stt_provider_id=stt_provider_id,
    )
    if not providers:
        return

    usage = _audio_usage_summary(web_backend_url.rstrip("/"))
    monthly_limit_cents = _numeric_usage_value(usage, "monthly_limit_cents")
    if monthly_limit_cents <= 0:
        raise OpenbaseCloudAudioSubscriptionError(OPENBASE_CLOUD_AUDIO_SUBSCRIBE_DETAIL)

    exhausted = [
        provider
        for provider in providers
        if _numeric_usage_value(usage, f"{provider}_remaining_cents") <= 0
    ]
    if exhausted:
        provider_names = _provider_names(exhausted)
        raise OpenbaseCloudAudioSubscriptionError(
            f"Openbase Cloud audio is out of {provider_names} credits for this "
            "month. Subscribe or upgrade at app.openbase.cloud, or switch "
            "voice settings to direct provider keys or local audio."
        )


def openbase_cloud_subscription_entitlement(
    *,
    web_backend_url: str = DEFAULT_WEB_BACKEND_URL,
    access_token: str | None = None,
) -> dict[str, object]:
    """Return Cloud-backed subscription state for paid local app features."""
    profile = _cloud_user_profile(
        web_backend_url.rstrip("/"),
        access_token=access_token,
    )
    if "active_subscription" in profile:
        has_active_subscription = _has_active_subscription_value(
            profile.get("active_subscription")
        )
    else:
        usage = _audio_usage_summary(
            web_backend_url.rstrip("/"),
            access_token=access_token,
        )
        has_active_subscription = _numeric_usage_value(usage, "monthly_limit_cents") > 0
    detail = "" if has_active_subscription else OPENBASE_CLOUD_SUBSCRIBE_DETAIL
    return {
        "has_active_subscription": has_active_subscription,
        "detail": detail,
    }


def _audio_usage_summary(
    web_backend_url: str,
    *,
    access_token: str | None = None,
) -> dict:
    return _cloud_json_get(
        web_backend_url,
        "/api/openbase/audio/usage/",
        access_token=access_token,
    )


def _cloud_user_profile(web_backend_url: str, *, access_token: str | None) -> dict:
    return _cloud_json_get(web_backend_url, "/api/users/me/", access_token=access_token)


def _cloud_json_get(
    web_backend_url: str,
    path: str,
    *,
    access_token: str | None = None,
) -> dict:
    if access_token is None:
        token_manager = get_token_manager(web_backend_url)
        try:
            access_token = token_manager.get_access_token()
        except AuthLoginRequiredError:
            raise
        except AuthTransientError:
            raise

    try:
        response = httpx.get(
            f"{web_backend_url}{path}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=10,
        )
    except httpx.HTTPError as exc:
        raise AuthTransientError(f"Cloud subscription check failed: {exc}") from exc

    if response.status_code == 401:
        raise AuthLoginRequiredError(
            "Openbase Cloud rejected the current login while checking subscription."
        )
    if response.status_code == 403:
        raise OpenbaseCloudAudioSubscriptionError(_response_detail(response))
    if response.status_code >= 500:
        raise AuthTransientError(
            f"Cloud subscription check failed with backend status {response.status_code}"
        )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise AuthTransientError(
            f"Cloud subscription check failed with backend status {response.status_code}: "
            f"{_response_detail(response)}"
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthTransientError(
            "Cloud subscription check returned invalid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise AuthTransientError(
            "Cloud subscription check returned an invalid payload."
        )
    return payload


def _required_cloud_audio_providers(
    *,
    tts_provider_id: str,
    stt_provider_id: str,
) -> set[str]:
    providers: set[str] = set()
    if tts_provider_id == OPENBASE_CLOUD_TTS_PROVIDER_ID:
        providers.add("cartesia")
    if stt_provider_id == OPENBASE_CLOUD_STT_PROVIDER_ID:
        providers.add("assemblyai")
    return providers


def _numeric_usage_value(payload: dict, key: str) -> float:
    value = payload.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _has_active_subscription_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        return normalized not in {"", "0", "false", "none", "null"}
    return value is not None


def _provider_names(providers: Iterable[str]) -> str:
    names = [
        "AssemblyAI" if provider == "assemblyai" else "Cartesia"
        for provider in providers
    ]
    return " and ".join(sorted(names))


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300].strip() or response.reason_phrase
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error")
        if detail:
            return str(detail)
    return str(payload)[:300]
