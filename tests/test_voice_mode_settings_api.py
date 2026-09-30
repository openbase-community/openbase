from __future__ import annotations

# ruff: noqa: E402, I001

import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli import dispatcher_config  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import voice_mode_settings  # noqa: E402


def _authenticated_request(method: str, data: dict | None = None):
    factory = APIRequestFactory()
    request_factory = {"GET": factory.get, "PUT": factory.put}[method]
    request = request_factory(
        "/api/settings/voice-mode/", data=data or {}, format="json"
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def test_voice_mode_defaults_to_dispatcher(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", tmp_path / "missing.json"
    )
    monkeypatch.delenv("OPENBASE_VOICE_MODE", raising=False)

    response = voice_mode_settings.voice_mode_settings(_authenticated_request("GET"))

    assert response.status_code == 200
    assert response.data["voice_mode"] == "dispatcher"
    assert response.data["default"] == "dispatcher"
    assert [option["id"] for option in response.data["options"]] == [
        "dispatcher",
        "direct",
    ]
    assert response.data["restart_required"] is False


def test_voice_mode_put_persists_direct(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text(
        json.dumps({"dispatcher_service_tier": "fast"}), encoding="utf-8"
    )
    monkeypatch.setattr(dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path)

    response = voice_mode_settings.voice_mode_settings(
        _authenticated_request("PUT", {"voice_mode": "direct"})
    )

    assert response.status_code == 200
    assert response.data["voice_mode"] == "direct"
    assert response.data["changed"] is True
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["voice_mode"] == "direct"
    # Existing keys survive the write.
    assert saved["dispatcher_service_tier"] == "fast"
    assert dispatcher_config.voice_mode(config_path) == "direct"

    unchanged = voice_mode_settings.voice_mode_settings(
        _authenticated_request("PUT", {"voice_mode": "direct"})
    )
    assert unchanged.data["changed"] is False


def test_voice_mode_rejects_unknown_value(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", tmp_path / "config.json"
    )

    response = voice_mode_settings.voice_mode_settings(
        _authenticated_request("PUT", {"voice_mode": "party"})
    )

    assert response.status_code == 400
