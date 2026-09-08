from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import env_settings  # noqa: E402


def _authenticated_request(method: str, path: str, data: dict | None = None):
    factory = APIRequestFactory()
    request_factory = {
        "GET": factory.get,
        "PUT": factory.put,
    }[method]
    request = request_factory(path, data=data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def test_env_settings_reads_active_values(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "CODEX_MODEL=gpt-5.5",
                "CARTESIA_API_KEY=secret-value",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(env_settings, "DEFAULT_ENV_FILE_PATH", env_file)

    response = env_settings.env_settings(
        _authenticated_request("GET", "/api/settings/env/")
    )

    assert response.status_code == 200
    assert response.data["env_file_exists"] is True
    assert response.data["entries"] == [
        {"key": "CODEX_MODEL", "value": "gpt-5.5", "secret": False},
        {"key": "CARTESIA_API_KEY", "value": "secret-value", "secret": True},
    ]
    assert "SUPER_AGENTS_CLAUDE_PERMISSION_MODE" in response.data["common_keys"]


def test_env_settings_updates_adds_renames_and_deletes(
    monkeypatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# keep me",
                "CODEX_MODEL=gpt-5.5",
                "OLD_KEY=old",
                "REMOVE_ME=bye",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(env_settings, "DEFAULT_ENV_FILE_PATH", env_file)

    response = env_settings.env_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/env/",
            {
                "entries": [
                    {"key": "CODEX_MODEL", "value": "gpt-5.6"},
                    {"key": "NEW_KEY", "value": "new value"},
                    {"key": "RENAMED_KEY", "value": "renamed"},
                ],
                "deleted_keys": ["OLD_KEY", "REMOVE_ME"],
            },
        )
    )

    assert response.status_code == 200
    assert response.data["changed"] is True
    text = env_file.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "CODEX_MODEL=gpt-5.6" in text
    assert 'NEW_KEY="new value"' in text
    assert "RENAMED_KEY=renamed" in text
    assert "OLD_KEY" not in text
    assert "REMOVE_ME" not in text


def test_env_settings_rejects_duplicate_keys(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(env_settings, "DEFAULT_ENV_FILE_PATH", tmp_path / ".env")

    response = env_settings.env_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/env/",
            {
                "entries": [
                    {"key": "CODEX_MODEL", "value": "gpt-5.5"},
                    {"key": "CODEX_MODEL", "value": "gpt-5.6"},
                ],
            },
        )
    )

    assert response.status_code == 400
