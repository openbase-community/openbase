from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli import dispatcher_config  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import voice_model_settings  # noqa: E402
from openbase_coder_cli.voice_models import (  # noqa: E402
    GPT_LIVE_VOICE_MODEL_ID,
    PIPELINE_VOICE_MODEL_ID,
    VOICE_MODEL_ENV_KEY,
)


@pytest.fixture(autouse=True)
def _isolated_voice_config(monkeypatch, tmp_path: Path) -> Path:
    config_path = tmp_path / "dispatcher-config.json"
    monkeypatch.setattr(dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path)
    monkeypatch.delenv(VOICE_MODEL_ENV_KEY, raising=False)
    return config_path


def _authenticated_request(method: str, path: str, data: dict | None = None):
    factory = APIRequestFactory()
    request_factory = {
        "GET": factory.get,
        "PUT": factory.put,
    }[method]
    request = request_factory(path, data=data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def _get():
    return voice_model_settings.voice_model_settings(
        _authenticated_request("GET", "/api/settings/voice-model/")
    )


def _put(data: dict):
    return voice_model_settings.voice_model_settings(
        _authenticated_request("PUT", "/api/settings/voice-model/", data)
    )


def _usage():
    return voice_model_settings.voice_model_usage(
        _authenticated_request("GET", "/api/settings/voice-model/usage/")
    )


def test_usage_passes_authoritative_budget_and_uses_selected_cloud(monkeypatch):
    budget = {
        "warning_id": "account-period",
        "low": True,
        "shared": True,
        "remaining_percent": 10,
    }
    monkeypatch.setattr(
        voice_model_settings, "web_backend_url", lambda: "https://cloud.example/"
    )

    def fetch(url):
        assert url == "https://cloud.example"
        return {"live_voice_budget": budget}

    monkeypatch.setattr(voice_model_settings, "audio_usage_summary", fetch)
    response = _usage().data
    assert response["model"] == GPT_LIVE_VOICE_MODEL_ID
    assert response["budget"]["remaining_percent"] == 10
    assert response["budget"]["low"] is True
    first_id = response["budget"]["warning_id"]
    assert _usage().data["budget"]["warning_id"] == first_id
    monkeypatch.setattr(
        voice_model_settings, "web_backend_url", lambda: "https://other-cloud.example"
    )
    monkeypatch.setattr(
        voice_model_settings,
        "audio_usage_summary",
        lambda url: {"live_voice_budget": budget},
    )
    assert _usage().data["budget"]["warning_id"] != first_id


def test_pipeline_usage_does_not_contact_cloud(monkeypatch):
    dispatcher_config.set_voice_model("pipeline")

    def unexpected(url):
        pytest.fail("Classic voice should not poll Cloud usage")

    monkeypatch.setattr(voice_model_settings, "audio_usage_summary", unexpected)
    assert _usage().data == {"model": "pipeline", "budget": None}


@pytest.mark.parametrize(
    "failure",
    [
        voice_model_settings.AuthLoginRequiredError,
        voice_model_settings.AuthTransientError,
        voice_model_settings.OpenbaseCloudAudioSubscriptionError,
    ],
)
def test_unavailable_usage_keeps_settings_working(monkeypatch, failure):
    def unavailable(url):
        raise failure("unavailable")

    monkeypatch.setattr(voice_model_settings, "audio_usage_summary", unavailable)
    assert _usage().data["budget"] is None
    assert _put({"model": "pipeline"}).status_code == 200


def test_older_cloud_does_not_guess_from_provider_spend(monkeypatch):
    monkeypatch.setattr(
        voice_model_settings,
        "audio_usage_summary",
        lambda url: {"live_voice_spend_percent": 95},
    )
    assert _usage().data["budget"] is None


def test_voice_model_settings_defaults_to_gpt_live(
    _isolated_voice_config: Path,
) -> None:
    response = _get()

    assert response.status_code == 200
    assert response.data["model"] == GPT_LIVE_VOICE_MODEL_ID
    assert response.data["engine"] == "live"
    assert response.data["default"] == GPT_LIVE_VOICE_MODEL_ID
    assert response.data["pipeline_settings_relevant"] is False
    assert response.data["changed"] is False
    assert response.data["restart_required"] is False
    assert response.data["applies_hint"] == (
        "The new voice model applies to the next voice call."
    )
    assert response.data["config_path"] == str(_isolated_voice_config)
    # GPT-Live always runs through Openbase Cloud: there is no provider choice.
    assert "live_voice_provider" not in response.data
    assert "live_voice_provider_options" not in response.data

    options = response.data["options"]
    assert [option["id"] for option in options] == [
        GPT_LIVE_VOICE_MODEL_ID,
        PIPELINE_VOICE_MODEL_ID,
    ]
    assert [option["is_default"] for option in options] == [True, False]
    assert {option["engine"] for option in options} == {"live", "pipeline"}
    assert all({"id", "label", "description"} <= option.keys() for option in options)
    assert "Openbase Cloud" in options[0]["description"]
    assert "key" not in options[0]["description"].lower()


def test_voice_model_settings_reads_config(_isolated_voice_config: Path) -> None:
    _isolated_voice_config.write_text(
        json.dumps({"voice_model": "pipeline"}), encoding="utf-8"
    )

    response = _get()

    assert response.status_code == 200
    assert response.data["model"] == PIPELINE_VOICE_MODEL_ID
    assert response.data["engine"] == "pipeline"
    assert response.data["pipeline_settings_relevant"] is True


def test_voice_model_settings_persists_model(_isolated_voice_config: Path) -> None:
    response = _put({"model": "classic"})

    assert response.status_code == 200
    assert response.data["model"] == PIPELINE_VOICE_MODEL_ID
    assert response.data["engine"] == "pipeline"
    assert response.data["pipeline_settings_relevant"] is True
    assert response.data["changed"] is True
    assert response.data["restart_required"] is False
    payload = json.loads(_isolated_voice_config.read_text(encoding="utf-8"))
    assert payload["voice_model"] == PIPELINE_VOICE_MODEL_ID


def test_voice_model_settings_reports_unchanged_for_same_model(
    _isolated_voice_config: Path,
) -> None:
    response = _put({"model": "gpt-live-1"})

    assert response.status_code == 200
    assert response.data["model"] == GPT_LIVE_VOICE_MODEL_ID
    assert response.data["changed"] is False
    payload = json.loads(_isolated_voice_config.read_text(encoding="utf-8"))
    assert payload["voice_model"] == GPT_LIVE_VOICE_MODEL_ID


def test_voice_model_settings_rejects_unknown_model(
    _isolated_voice_config: Path,
) -> None:
    response = _put({"model": "gemini-3.8-live"})

    assert response.status_code == 400
    assert response.data["error"] == "Voice model must be one of: gpt-live-1, pipeline."
    assert not _isolated_voice_config.exists()


def test_voice_model_settings_rejects_missing_model(
    _isolated_voice_config: Path,
) -> None:
    response = _put({})

    assert response.status_code == 400
    assert "model" in response.data
    assert not _isolated_voice_config.exists()
