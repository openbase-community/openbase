from __future__ import annotations

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
from openbase_coder_cli.openbase_coder_cli_app import reasoning_settings  # noqa: E402


def _authenticated_request(method: str, path: str, data: dict | None = None):
    factory = APIRequestFactory()
    request_factory = {
        "GET": factory.get,
        "PUT": factory.put,
    }[method]
    request = request_factory(path, data=data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def test_reasoning_settings_reads_config(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text(
        json.dumps(
            {
                "dispatcher_reasoning_effort": "low",
                "super_agents_reasoning_effort": "high",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path)

    response = reasoning_settings.reasoning_settings(
        _authenticated_request("GET", "/api/settings/reasoning/")
    )

    assert response.status_code == 200
    assert response.data["dispatcher_reasoning_effort"] == "low"
    assert response.data["super_agents_reasoning_effort"] == "high"
    assert response.data["effective"] == {
        "dispatcher_reasoning_effort": "low",
        "super_agents_reasoning_effort": "high",
    }
    assert response.data["options"] == ["low", "medium", "high", "xhigh"]
    assert response.data["config_exists"] is True
    assert response.data["restart_required"] is False


def test_reasoning_settings_persists_config(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    monkeypatch.setattr(dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path)

    response = reasoning_settings.reasoning_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/reasoning/",
            {
                "dispatcher_reasoning_effort": "medium",
                "super_agents_reasoning_effort": "xhigh",
            },
        )
    )

    assert response.status_code == 200
    assert response.data["changed"] is True
    assert response.data["restart_hint"] == "Reasoning changes apply to the next turn."
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["dispatcher_reasoning_effort"] == "medium"
    assert payload["super_agents_reasoning_effort"] == "xhigh"


def test_reasoning_settings_rejects_invalid_effort(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    monkeypatch.setattr(dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path)

    response = reasoning_settings.reasoning_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/reasoning/",
            {
                "dispatcher_reasoning_effort": "extreme",
                "super_agents_reasoning_effort": "high",
            },
        )
    )

    assert response.status_code == 400
    assert "dispatcher_reasoning_effort" in response.data
    assert not config_path.exists()
