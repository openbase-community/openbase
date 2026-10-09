from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from openbase_coder_cli import cloud_models, dispatcher_config
from openbase_coder_cli.cloud_models import CATALOG_UNAVAILABLE_REASON
from openbase_coder_cli.config import cloud_audio
from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
)


def catalog(paid=False):
    return {
        "data": [
            {
                "id": model,
                "available": paid or alias == "haiku",
                "unavailable_reason": None
                if paid or alias == "haiku"
                else "Requires a paid plan.",
            }
            for alias, model in cloud_models.OPENBASE_CLOUD_CLAUDE_MODEL_MAP.items()
        ]
    }


@pytest.mark.parametrize("paid", [False, True])
def test_cloud_policy_is_applied_to_each_alias(monkeypatch, paid):
    get = Mock(return_value=catalog(paid))
    monkeypatch.setattr(cloud_models, "_cloud_json_get", get)
    monkeypatch.setenv(
        "OPENBASE_CODER_CLI_WEB_BACKEND_URL", "https://app-staging.openbase.cloud"
    )
    options = {
        option["id"]: option
        for option in dispatcher_config.combined_model_options("cloud")
    }
    get.assert_called_once_with(
        "https://app-staging.openbase.cloud", "/api/openbase/llm/anthropic/v1/models/"
    )
    for alias in ("haiku", "sonnet", "opus", "fable"):
        assert options[alias]["available"] == (paid or alias == "haiku")
        assert bool(options[alias]["unavailable_reason"]) != options[alias]["available"]
    assert not options["gpt-5.5"]["available"]


@pytest.mark.parametrize(
    "payload", [{}, {"data": None}, {"data": [None, {}, {"id": "claude-fable-5"}]}]
)
def test_missing_policy_does_not_advertise_unverified_models(monkeypatch, payload):
    monkeypatch.setattr(cloud_models, "_cloud_json_get", lambda *args: payload)
    assert set(cloud_models.cloud_model_availability().values()) == {
        CATALOG_UNAVAILABLE_REASON
    }


@pytest.mark.parametrize(
    "error", [AuthLoginRequiredError("login"), AuthTransientError("offline")]
)
def test_unreachable_catalog_gives_retry_reason_not_plan_denial(monkeypatch, error):
    monkeypatch.setattr(cloud_models, "_cloud_json_get", Mock(side_effect=error))
    assert set(cloud_models.cloud_model_availability().values()) == {
        CATALOG_UNAVAILABLE_REASON
    }


def test_policy_refreshes_after_upgrade_and_account_change(monkeypatch):
    monkeypatch.setattr(
        cloud_models,
        "_cloud_json_get",
        Mock(side_effect=[catalog(), catalog(True), catalog()]),
    )
    assert cloud_models.cloud_model_availability()["opus"]
    assert cloud_models.cloud_model_availability()["opus"] is None
    assert cloud_models.cloud_model_availability()["opus"]


def test_catalog_accepts_machine_only_workspace_auth(monkeypatch):
    monkeypatch.setenv(
        "OPENBASE_CODER_CLI_WEB_BACKEND_URL", "https://app-staging.openbase.cloud"
    )
    monkeypatch.setattr(
        cloud_audio,
        "get_token_manager",
        lambda _: SimpleNamespace(
            get_access_token=Mock(side_effect=AuthLoginRequiredError("no user login"))
        ),
    )
    monkeypatch.setattr(
        cloud_audio,
        "MachineTokenManager",
        lambda _: SimpleNamespace(
            has_cached_token=lambda: True,
            get_machine_token=lambda: "test-machine-token",
        ),
    )
    get = Mock(
        return_value=httpx.Response(
            200, json=catalog(), request=httpx.Request("GET", "https://example.invalid")
        )
    )
    monkeypatch.setattr(cloud_audio.httpx, "get", get)
    assert cloud_models.cloud_model_availability()["haiku"] is None
    assert (
        get.call_args.kwargs["headers"]["Authorization"] == "Bearer test-machine-token"
    )


def test_local_catalog_does_not_contact_cloud(monkeypatch):
    get = Mock(side_effect=AssertionError("unexpected Cloud request"))
    monkeypatch.setattr(cloud_models, "_cloud_json_get", get)
    monkeypatch.setattr(dispatcher_config, "engine_unavailable_reason", lambda *_: None)
    assert all(
        option["available"]
        for option in dispatcher_config.combined_model_options("local")
    )
    get.assert_not_called()


def test_sonnet_selection_tracks_catalog_upgrade_and_downgrade(monkeypatch):
    monkeypatch.setattr(
        cloud_models,
        "_cloud_json_get",
        Mock(side_effect=[catalog(), catalog(True), catalog()]),
    )
    assert cloud_models.cloud_model_availability()["sonnet"] == "Requires a paid plan."
    assert cloud_models.cloud_model_availability()["sonnet"] is None
    assert cloud_models.cloud_model_availability()["sonnet"] == "Requires a paid plan."
