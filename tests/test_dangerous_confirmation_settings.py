from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace


def _setup_django():
    os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings"
    )

    import django

    django.setup()


def _authenticated_request(method: str, path: str, payload: dict | None = None):
    from rest_framework.test import APIRequestFactory, force_authenticate

    factory = APIRequestFactory()
    request = getattr(factory, method.lower())(path, payload or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def test_dangerous_confirmation_settings_defaults_to_existing_phrase(
    tmp_path: Path, monkeypatch
) -> None:
    _setup_django()

    from openbase_coder_cli.openbase_coder_cli_app import services_views
    from openbase_coder_cli.services import console_settings

    monkeypatch.setattr(
        console_settings,
        "CONSOLE_SETTINGS_JSON_PATH",
        tmp_path / "console-settings.json",
    )

    response = services_views.dangerous_confirmation_settings(
        _authenticated_request("GET", "/api/settings/dangerous-confirmation/")
    )

    assert response.status_code == 200
    assert response.data["dangerous_confirmation_phrase"] == "yes, proceed"
    assert response.data["default_dangerous_confirmation_phrase"] == "yes, proceed"
    assert response.data["user_address_name"] == "there"
    assert response.data["default_user_address_name"] == "there"


def test_dangerous_confirmation_settings_saves_phrase_and_refreshes(
    tmp_path: Path, monkeypatch
) -> None:
    _setup_django()

    from openbase_coder_cli.openbase_coder_cli_app import services_views
    from openbase_coder_cli.services import console_settings

    refreshed = []
    monkeypatch.setattr(
        console_settings,
        "CONSOLE_SETTINGS_JSON_PATH",
        tmp_path / "console-settings.json",
    )
    monkeypatch.setattr(
        services_views,
        "refresh_openbase_instruction_files_from_installation",
        lambda: refreshed.append("refresh") or True,
    )

    response = services_views.dangerous_confirmation_settings(
        _authenticated_request(
            "PATCH",
            "/api/settings/dangerous-confirmation/",
            {
                "dangerous_confirmation_phrase": "ship it",
                "user_address_name": "Sam",
            },
        )
    )

    assert response.status_code == 200
    assert response.data["dangerous_confirmation_phrase"] == "ship it"
    assert response.data["user_address_name"] == "Sam"
    assert response.data["refreshed"] is True
    assert refreshed == ["refresh"]
    assert console_settings.get_dangerous_confirmation_phrase() == "ship it"
    assert console_settings.get_user_address_name() == "Sam"


def test_keep_awake_settings_defaults_to_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    _setup_django()

    from openbase_coder_cli.openbase_coder_cli_app import services_views
    from openbase_coder_cli.services import console_settings

    monkeypatch.setattr(
        console_settings,
        "CONSOLE_SETTINGS_JSON_PATH",
        tmp_path / "console-settings.json",
    )

    response = services_views.keep_awake_settings(
        _authenticated_request("GET", "/api/settings/keep-awake/")
    )

    assert response.status_code == 200
    assert response.data["keep_system_awake"] is False
    assert response.data["default_keep_system_awake"] is False
    assert response.data["restart_required"] is True


def test_keep_awake_settings_saves_flag(tmp_path: Path, monkeypatch) -> None:
    _setup_django()

    from openbase_coder_cli.openbase_coder_cli_app import services_views
    from openbase_coder_cli.services import console_settings

    monkeypatch.setattr(
        console_settings,
        "CONSOLE_SETTINGS_JSON_PATH",
        tmp_path / "console-settings.json",
    )

    response = services_views.keep_awake_settings(
        _authenticated_request(
            "PATCH",
            "/api/settings/keep-awake/",
            {"keep_system_awake": False},
        )
    )

    assert response.status_code == 200
    assert response.data["keep_system_awake"] is False
    assert console_settings.get_keep_system_awake_enabled() is False
