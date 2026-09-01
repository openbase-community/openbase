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
from openbase_coder_cli.openbase_coder_cli_app import service_tier_settings  # noqa: E402


def _authenticated_request(method: str, path: str, data: dict | None = None):
    factory = APIRequestFactory()
    request_factory = {
        "GET": factory.get,
        "PUT": factory.put,
    }[method]
    request = request_factory(path, data=data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def test_service_tier_settings_reads_config(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    env_file = tmp_path / ".env"
    config_path.write_text(
        json.dumps(
            {
                "dispatcher_service_tier": "standard",
                "super_agents_service_tier": "fast",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path)
    monkeypatch.setattr(service_tier_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        service_tier_settings, "configured_execution_backend", lambda: "codex"
    )

    response = service_tier_settings.service_tier_settings(
        _authenticated_request("GET", "/api/settings/service-tier/")
    )

    assert response.status_code == 200
    assert response.data["editable"] is True
    assert response.data["not_editable_reason"] is None
    assert response.data["dispatcher_service_tier"] == "standard"
    assert response.data["super_agents_service_tier"] == "fast"
    assert response.data["effective"] == {
        "dispatcher_service_tier": "standard",
        "super_agents_service_tier": "fast",
    }
    assert [option["id"] for option in response.data["options"]] == [
        "fast",
        "standard",
    ]
    assert response.data["restart_required"] is False


def test_service_tier_settings_persists_config_and_env(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP_ME=1\nCODEX_SERVICE_TIER=fast\n", encoding="utf-8")
    # Importing livekit_agent.config elsewhere in the suite loads ~/.openbase/.env
    # into os.environ, which outranks the env file when resolving the current tier.
    monkeypatch.delenv("CODEX_SERVICE_TIER", raising=False)
    monkeypatch.delenv("DISPATCHER_SERVICE_TIER", raising=False)
    monkeypatch.delenv("SUPER_AGENTS_SERVICE_TIER", raising=False)
    monkeypatch.setattr(dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path)
    monkeypatch.setattr(dispatcher_config, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(service_tier_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        service_tier_settings, "configured_execution_backend", lambda: "codex"
    )

    response = service_tier_settings.service_tier_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/service-tier/",
            {
                "dispatcher_service_tier": "standard",
                "super_agents_service_tier": "fast",
            },
        )
    )

    assert response.status_code == 200
    assert response.data["dispatcher_service_tier"] == "standard"
    assert response.data["super_agents_service_tier"] == "fast"
    assert response.data["changed"] is True
    assert response.data["restart_required"] is True
    assert "Codex app-server" in response.data["restart_hint"]
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["dispatcher_service_tier"] == "standard"
    assert payload["super_agents_service_tier"] == "fast"
    env_content = env_file.read_text(encoding="utf-8")
    assert "KEEP_ME=1" in env_content
    # App-server ambient default follows the Super Agents lane.
    assert "CODEX_SERVICE_TIER=fast" in env_content
    assert "DISPATCHER_SERVICE_TIER=standard" in env_content
    assert "SUPER_AGENTS_SERVICE_TIER=fast" in env_content


def test_service_tier_settings_rejects_invalid_tier(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    env_file = tmp_path / ".env"
    monkeypatch.setattr(dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path)
    monkeypatch.setattr(service_tier_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        service_tier_settings, "configured_execution_backend", lambda: "codex"
    )

    response = service_tier_settings.service_tier_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/service-tier/",
            {"dispatcher_service_tier": "turbo"},
        )
    )

    assert response.status_code == 400
    assert "dispatcher_service_tier" in response.data
    assert not config_path.exists()
    assert not env_file.exists()


def test_service_tier_settings_not_editable_on_claude_backend(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    env_file = tmp_path / ".env"
    monkeypatch.setattr(dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path)
    monkeypatch.setattr(service_tier_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        service_tier_settings, "configured_execution_backend", lambda: "claude_code"
    )

    response = service_tier_settings.service_tier_settings(
        _authenticated_request("GET", "/api/settings/service-tier/")
    )

    assert response.status_code == 200
    assert response.data["editable"] is False
    assert (
        response.data["not_editable_reason"]
        == service_tier_settings.CLAUDE_BACKEND_SERVICE_TIER_ERROR
    )


def test_service_tier_settings_rejects_writes_on_claude_backend(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    env_file = tmp_path / ".env"
    monkeypatch.setattr(dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path)
    monkeypatch.setattr(service_tier_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        service_tier_settings, "configured_execution_backend", lambda: "claude_code"
    )

    response = service_tier_settings.service_tier_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/service-tier/",
            {
                "dispatcher_service_tier": "fast",
                "super_agents_service_tier": "fast",
            },
        )
    )

    assert response.status_code == 400
    assert (
        response.data["error"]
        == service_tier_settings.CLAUDE_BACKEND_SERVICE_TIER_ERROR
    )
    assert not config_path.exists()
    assert not env_file.exists()
