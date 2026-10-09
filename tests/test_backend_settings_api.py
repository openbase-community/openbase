from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
import pytest  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli import (  # noqa: E402
    claude_auth,
    cloud_models,
    dispatcher_config,
)
from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    backend_settings,
    model_settings,
)
from openbase_coder_cli.services import selection  # noqa: E402


def _install_with(
    monkeypatch, *, backends: tuple[str, ...], claude_logged_in: bool
) -> None:
    """Pin the two engine-availability checks the model picker consults.

    ``backends`` are the coding backends setup installed services for (the
    Codex app-server only exists on a codex backend); ``claude_logged_in``
    is what ``claude auth status`` would report.
    """
    monkeypatch.setattr(
        selection,
        "service_supports_configured_backends",
        lambda service: any(service.supports_backend(b) for b in backends),
    )
    monkeypatch.setattr(
        claude_auth,
        "claude_auth_status",
        lambda **_: SimpleNamespace(
            logged_in=claude_logged_in, raw_output="", returncode=0
        ),
    )


@pytest.fixture(autouse=True)
def _both_engines_available(monkeypatch) -> None:
    monkeypatch.setattr(
        cloud_models,
        "cloud_model_availability",
        lambda: dict.fromkeys(("haiku", "sonnet", "opus", "fable")),
    )
    # Never consult the real install or shell out to ``claude``; tests that
    # exercise a partial install override this explicitly.
    _install_with(monkeypatch, backends=("codex", "claude_code"), claude_logged_in=True)


def _authenticated_request(method: str, path: str, data: dict | None = None):
    factory = APIRequestFactory()
    request_factory = {
        "GET": factory.get,
        "POST": factory.post,
        "PUT": factory.put,
    }[method]
    request = request_factory(path, data=data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def test_coding_backend_settings_defaults_when_env_file_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)

    response = backend_settings.coding_backend_settings(
        _authenticated_request("GET", "/api/settings/coding-backend/")
    )

    assert response.status_code == 200
    assert response.data["backend"] == "codex"
    assert response.data["default_backend"] == "codex"
    assert response.data["env_file_exists"] is False
    assert response.data["restart_required"] is False
    assert [option["id"] for option in response.data["supported_backends"]] == [
        "codex",
        "openbase_cloud",
        "claude_code",
    ]


def test_coding_backend_settings_persists_openbase_cloud_selection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP_ME=1\nOPENBASE_CODEX_BACKEND=codex\n", encoding="utf-8")
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)

    response = backend_settings.coding_backend_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/coding-backend/",
            {"backend": "openbase_cloud"},
        )
    )

    assert response.status_code == 200
    assert response.data["backend"] == "openbase_cloud"
    assert response.data["configured_backend"] == "openbase_cloud"
    assert response.data["execution_backend"] == "claude_code"
    assert response.data["codex_provider"] == "direct"
    assert response.data["claude_provider"] == "openbase_cloud"
    assert "Openbase Cloud model proxy" in response.data["backend_note"]
    assert response.data["changed"] is True
    assert response.data["restart_required"] is True
    assert "dispatcher/MCP host" in response.data["restart_hint"]
    content = env_file.read_text(encoding="utf-8")
    assert "KEEP_ME=1" in content
    assert "OPENBASE_CODEX_BACKEND=codex" in content
    assert "OPENBASE_CODING_BACKEND=openbase_cloud" in content
    assert not (tmp_path / "codex_home" / "config.toml").exists()


def test_backend_model_settings_lists_claude_fable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=claude-code\n", encoding="utf-8")
    monkeypatch.setattr(model_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "DEFAULT_ENV_FILE_PATH",
        env_file,
    )
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "CODEX_DISPATCHER_CONFIG_PATH",
        tmp_path / "dispatcher-config.json",
    )

    response = model_settings.backend_model_settings(
        _authenticated_request("GET", "/api/settings/backend-model/")
    )

    assert response.status_code == 200
    assert response.data["backend"] == "claude_code"
    assert response.data["location"] == "local"
    assert [option["id"] for option in response.data["options"]] == [
        "fable",
        "opus",
        "sonnet",
        "haiku",
        "gpt-5.5",
        "gpt-5",
        "sol",
        "astra",
    ]
    # Locally, with Codex services installed and Claude logged in, both
    # engines are available; the model picks the engine.
    assert all(option["available"] for option in response.data["options"])
    assert all(
        option["unavailable_reason"] is None for option in response.data["options"]
    )
    assert response.data["options"][0]["engine"] == "claude"
    codex_engines = {
        option["id"]: option["engine"]
        for option in response.data["options"]
        if option["id"] in {"gpt-5.5", "gpt-5", "sol", "astra"}
    }
    assert set(codex_engines.values()) == {"codex"}


def test_backend_model_settings_lists_openbase_cloud_claude_model(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=openbase_cloud\n", encoding="utf-8")
    monkeypatch.setattr(model_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "DEFAULT_ENV_FILE_PATH",
        env_file,
    )
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "CODEX_DISPATCHER_CONFIG_PATH",
        tmp_path / "dispatcher-config.json",
    )

    response = model_settings.backend_model_settings(
        _authenticated_request("GET", "/api/settings/backend-model/")
    )

    assert response.status_code == 200
    assert response.data["backend"] == "openbase_cloud"
    assert response.data["location"] == "cloud"
    assert [option["id"] for option in response.data["options"]] == [
        "haiku",
        "sonnet",
        "opus",
        "fable",
        "gpt-5.5",
        "gpt-5",
        "sol",
        "astra",
    ]
    assert response.data["options"][0]["is_default"] is True
    assert response.data["options"][0]["label"] == "Claude Haiku"
    assert (
        "Trial accounts run Claude Haiku" in response.data["options"][1]["description"]
    )
    # Codex is read-only on Openbase Cloud: listed, but not selectable.
    availability = {
        option["id"]: option["available"] for option in response.data["options"]
    }
    assert availability["gpt-5.5"] is False
    assert availability["sol"] is False
    assert availability["astra"] is False
    assert availability["fable"] is True
    reasons = {
        option["id"]: option["unavailable_reason"]
        for option in response.data["options"]
    }
    assert reasons["gpt-5.5"] == dispatcher_config.CODEX_CLOUD_UNAVAILABLE_REASON
    assert reasons["fable"] is None


def test_backend_model_settings_accepts_fable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    config_path = tmp_path / "dispatcher-config.json"
    env_file.write_text("OPENBASE_CODING_BACKEND=claude-code\n", encoding="utf-8")
    monkeypatch.setattr(model_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "DEFAULT_ENV_FILE_PATH",
        env_file,
    )
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "CODEX_DISPATCHER_CONFIG_PATH",
        config_path,
    )

    response = model_settings.backend_model_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/backend-model/",
            {"role": "super_agents", "model": "fable"},
        )
    )

    assert response.status_code == 200
    assert response.data["models"]["super_agents"] == "fable"


def test_backend_model_settings_updates_dispatcher_role(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    config_path = tmp_path / "dispatcher-config.json"
    env_file.write_text("OPENBASE_CODING_BACKEND=codex\n", encoding="utf-8")
    monkeypatch.setattr(model_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "DEFAULT_ENV_FILE_PATH",
        env_file,
    )
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "CODEX_DISPATCHER_CONFIG_PATH",
        config_path,
    )

    response = model_settings.backend_model_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/backend-model/",
            {"role": "dispatcher", "model": "gpt-5.5"},
        )
    )

    assert response.status_code == 200
    assert response.data["models"]["dispatcher"] == "gpt-5.5"
    assert response.data["roles"]["dispatcher"]["engine"] == "codex"
    assert response.data["restart_required"] is True
    # The dispatcher role never rewrites the primary backend.
    assert "OPENBASE_CODING_BACKEND=codex" in env_file.read_text(encoding="utf-8")


def test_backend_model_settings_accepts_other_codex_models(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Every Codex option the runtime supports is accepted locally, classified
    as the codex engine, and written under the codex backend identity."""
    env_file = tmp_path / ".env"
    config_path = tmp_path / "dispatcher-config.json"
    env_file.write_text("OPENBASE_CODING_BACKEND=codex\n", encoding="utf-8")
    monkeypatch.setattr(model_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "DEFAULT_ENV_FILE_PATH",
        env_file,
    )
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "CODEX_DISPATCHER_CONFIG_PATH",
        config_path,
    )

    for model in ("gpt-5", "sol", "astra"):
        response = model_settings.backend_model_settings(
            _authenticated_request(
                "PUT",
                "/api/settings/backend-model/",
                {"role": "super_agents", "model": model},
            )
        )

        assert response.status_code == 200, response.data
        assert response.data["models"]["super_agents"] == model
        assert response.data["roles"]["super_agents"]["engine"] == "codex"
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        assert payload["backend_models"]["codex"]["super_agents"] == model
        assert payload["role_models"]["super_agents"] == model
        assert "OPENBASE_CODING_BACKEND=codex" in env_file.read_text(encoding="utf-8")


def test_backend_model_settings_rejects_codex_models_on_cloud(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    config_path = tmp_path / "dispatcher-config.json"
    env_file.write_text("OPENBASE_CODING_BACKEND=openbase_cloud\n", encoding="utf-8")
    monkeypatch.setattr(model_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "DEFAULT_ENV_FILE_PATH",
        env_file,
    )
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "CODEX_DISPATCHER_CONFIG_PATH",
        config_path,
    )

    response = model_settings.backend_model_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/backend-model/",
            {"role": "super_agents", "model": "sol"},
        )
    )

    assert response.status_code == 400
    assert response.data["error"] == dispatcher_config.CODEX_CLOUD_UNAVAILABLE_REASON
    assert not config_path.exists()


def _local_model_settings(monkeypatch, tmp_path: Path, backend: str) -> Path:
    env_file = tmp_path / ".env"
    config_path = tmp_path / "dispatcher-config.json"
    env_file.write_text(f"OPENBASE_CODING_BACKEND={backend}\n", encoding="utf-8")
    monkeypatch.setattr(model_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(model_settings.dispatcher_config, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        model_settings.dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path
    )
    return config_path


def test_backend_model_settings_marks_codex_unavailable_on_claude_only_install(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A --backend claude-code install never got the Codex app-server
    services, so every Codex model is listed but unavailable, with a reason."""
    _local_model_settings(monkeypatch, tmp_path, "claude-code")
    _install_with(monkeypatch, backends=("claude_code",), claude_logged_in=True)

    response = model_settings.backend_model_settings(
        _authenticated_request("GET", "/api/settings/backend-model/")
    )

    assert response.status_code == 200
    assert response.data["location"] == "local"
    options = {option["id"]: option for option in response.data["options"]}
    for model in ("gpt-5.5", "gpt-5", "sol", "astra"):
        assert options[model]["available"] is False
        assert (
            options[model]["unavailable_reason"]
            == dispatcher_config.CODEX_NOT_INSTALLED_REASON
        )
    for model in ("fable", "opus", "sonnet", "haiku"):
        assert options[model]["available"] is True
        assert options[model]["unavailable_reason"] is None


def test_backend_model_settings_marks_claude_unavailable_without_login(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A codex-only install with no Claude Code login lists Claude models as
    unavailable, with the login hint as the reason."""
    _local_model_settings(monkeypatch, tmp_path, "codex")
    _install_with(monkeypatch, backends=("codex",), claude_logged_in=False)

    response = model_settings.backend_model_settings(
        _authenticated_request("GET", "/api/settings/backend-model/")
    )

    assert response.status_code == 200
    options = {option["id"]: option for option in response.data["options"]}
    for model in ("fable", "opus", "sonnet", "haiku"):
        assert options[model]["available"] is False
        assert (
            options[model]["unavailable_reason"]
            == dispatcher_config.CLAUDE_NOT_LOGGED_IN_REASON
        )
    for model in ("gpt-5.5", "gpt-5", "sol", "astra"):
        assert options[model]["available"] is True
        assert options[model]["unavailable_reason"] is None


def test_backend_model_settings_rejects_unavailable_model_with_reason(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """PUT of a listed-but-unavailable model is a 400 whose error is the
    option's own reason string, so the API and the picker agree."""
    config_path = _local_model_settings(monkeypatch, tmp_path, "claude-code")
    _install_with(monkeypatch, backends=("claude_code",), claude_logged_in=True)

    response = model_settings.backend_model_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/backend-model/",
            {"role": "dispatcher", "model": "gpt-5.5"},
        )
    )

    assert response.status_code == 400
    assert response.data["error"] == dispatcher_config.CODEX_NOT_INSTALLED_REASON
    assert not config_path.exists()

    _install_with(monkeypatch, backends=("codex",), claude_logged_in=False)
    response = model_settings.backend_model_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/backend-model/",
            {"role": "super_agents", "model": "fable"},
        )
    )

    assert response.status_code == 400
    assert response.data["error"] == dispatcher_config.CLAUDE_NOT_LOGGED_IN_REASON
    assert not config_path.exists()

    # A model that is not in the catalog at all keeps the generic message.
    response = model_settings.backend_model_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/backend-model/",
            {"role": "super_agents", "model": "surprise"},
        )
    )
    assert response.status_code == 400
    assert "Model must be one of" in response.data["error"]


def test_backend_model_settings_keeps_configured_model_listed_when_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A model chosen before the install lost its engine stays in the list
    (unavailable) so the picker can still show what is configured."""
    config_path = _local_model_settings(monkeypatch, tmp_path, "claude-code")
    config_path.write_text(
        json.dumps({"role_models": {"super_agents": "sol"}}), encoding="utf-8"
    )
    _install_with(monkeypatch, backends=("claude_code",), claude_logged_in=True)

    response = model_settings.backend_model_settings(
        _authenticated_request("GET", "/api/settings/backend-model/")
    )

    assert response.status_code == 200
    assert response.data["roles"]["super_agents"]["model"] == "sol"
    options = {option["id"]: option for option in response.data["options"]}
    assert options["sol"]["available"] is False
    assert options["sol"]["unavailable_reason"] == (
        dispatcher_config.CODEX_NOT_INSTALLED_REASON
    )


def test_super_agents_model_choice_updates_primary_backend(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Picking a Claude default super-agent model on a Codex-primary install
    moves the primary backend to the engine that runs the model."""
    env_file = tmp_path / ".env"
    config_path = tmp_path / "dispatcher-config.json"
    env_file.write_text("OPENBASE_CODING_BACKEND=codex\n", encoding="utf-8")
    monkeypatch.setattr(model_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "DEFAULT_ENV_FILE_PATH",
        env_file,
    )
    monkeypatch.setattr(
        model_settings.dispatcher_config,
        "CODEX_DISPATCHER_CONFIG_PATH",
        config_path,
    )

    response = model_settings.backend_model_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/backend-model/",
            {"role": "super_agents", "model": "fable"},
        )
    )

    assert response.status_code == 200
    assert response.data["roles"]["super_agents"]["engine"] == "claude"
    assert "OPENBASE_CODING_BACKEND=claude_code" in env_file.read_text(
        encoding="utf-8"
    )


def test_coding_backend_settings_persists_claude_code_selection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        backend_settings,
        "verified_claude_auth_status",
        lambda: SimpleNamespace(
            logged_in=True, raw_output='{"loggedIn": true}', returncode=0
        ),
    )

    response = backend_settings.coding_backend_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/coding-backend/",
            {"backend": "claude_code"},
        )
    )

    assert response.status_code == 200
    assert response.data["backend"] == "claude_code"
    assert response.data["claude_auth"]["logged_in"] is True
    assert response.data["claude_auth"]["command"] == "claude login"
    assert response.data["changed"] is True
    assert "Claude Code" in response.data["restart_hint"]
    assert "OPENBASE_CODING_BACKEND=claude_code" in env_file.read_text(encoding="utf-8")


def test_coding_backend_settings_rejects_unsupported_backend(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)

    response = backend_settings.coding_backend_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/coding-backend/",
            {"backend": "surprise"},
        )
    )

    assert response.status_code == 400
    assert "backend" in response.data
    assert not env_file.exists()


def test_codex_plugin_settings_reports_openbase_codex_plugins(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=codex\n", encoding="utf-8")
    codex_home = tmp_path / "codex_home"
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(backend_settings, "CODEX_HOME_DIR", codex_home)
    monkeypatch.setattr(
        backend_settings,
        "_codex_plugin_list",
        lambda: {
            "installed": [
                {
                    "pluginId": "computer-use@openai-bundled",
                    "installed": True,
                    "enabled": True,
                    "version": "1.0.857",
                }
            ],
            "available": [
                {
                    "pluginId": "chrome@openai-bundled",
                    "installed": False,
                    "enabled": False,
                    "version": "26.623.141536",
                }
            ],
        },
    )

    response = backend_settings.codex_plugin_settings(
        _authenticated_request("GET", "/api/settings/coding-backend/codex-plugins/")
    )

    assert response.status_code == 200
    assert response.data["backend"] == "codex"
    assert response.data["codex_home"] == str(codex_home)
    plugins = {item["id"]: item for item in response.data["plugins"]}
    assert plugins["computer-use"]["installed"] is True
    assert plugins["computer-use"]["enabled"] is True
    assert plugins["computer-use"]["version"] == "1.0.857"
    assert plugins["chrome"]["installed"] is False
    assert response.data["restart_required"] is False


def test_codex_plugin_settings_toggles_plugin_and_requests_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=codex\n", encoding="utf-8")
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    calls: list[tuple[str, bool]] = []
    installed = {"chrome@openai-bundled": False}

    def fake_plugin_list() -> dict:
        return {
            "installed": [
                {
                    "pluginId": plugin_id,
                    "installed": True,
                    "enabled": True,
                    "version": "26.623.141536",
                }
                for plugin_id, is_installed in installed.items()
                if is_installed
            ],
            "available": [
                {
                    "pluginId": plugin_id,
                    "installed": False,
                    "enabled": False,
                    "version": "26.623.141536",
                }
                for plugin_id, is_installed in installed.items()
                if not is_installed
            ],
        }

    def fake_set_plugin(plugin_name: str, enabled: bool) -> None:
        calls.append((plugin_name, enabled))
        installed[f"{plugin_name}@openai-bundled"] = enabled

    monkeypatch.setattr(backend_settings, "_codex_plugin_list", fake_plugin_list)
    monkeypatch.setattr(
        backend_settings,
        "_set_codex_plugin_enabled",
        fake_set_plugin,
    )

    response = backend_settings.codex_plugin_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/coding-backend/codex-plugins/",
            {"plugin": "chrome", "enabled": True},
        )
    )

    assert response.status_code == 200
    assert calls == [("chrome", True)]
    assert response.data["changed"] is True
    assert response.data["changed_plugin"] == "chrome"
    assert response.data["restart_required"] is True
    plugins = {item["id"]: item for item in response.data["plugins"]}
    assert plugins["chrome"]["installed"] is True


def test_codex_plugin_settings_rejects_unknown_plugin() -> None:
    response = backend_settings.codex_plugin_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/coding-backend/codex-plugins/",
            {"plugin": "surprise", "enabled": True},
        )
    )

    assert response.status_code == 400
    assert "plugin" in response.data


def test_claude_auth_settings_hidden_for_non_claude_backend(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=codex\n", encoding="utf-8")
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)

    response = backend_settings.claude_auth_settings(
        _authenticated_request("GET", "/api/settings/coding-backend/claude-auth/")
    )

    assert response.status_code == 400
    assert response.data["backend"] == "codex"


def test_claude_auth_settings_syncs_state_and_reports_status(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=claude-code\n", encoding="utf-8")
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        backend_settings,
        "verified_claude_auth_status",
        lambda: SimpleNamespace(
            logged_in=True, raw_output='{"loggedIn": true}', returncode=0
        ),
    )

    response = backend_settings.claude_auth_settings(
        _authenticated_request("POST", "/api/settings/coding-backend/claude-auth/")
    )

    assert response.status_code == 200
    assert response.data["command"] == "claude login"
    assert response.data["logged_in"] is True
    assert response.data["verified"] is True


def test_claude_plugin_settings_reports_enabled_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=claude-code\n", encoding="utf-8")
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        backend_settings.claude_plugins, "computer_use_enabled", lambda: True
    )

    response = backend_settings.claude_plugin_settings(
        _authenticated_request("GET", "/api/settings/coding-backend/claude-plugins/")
    )

    assert response.status_code == 200
    assert response.data["backend"] == "claude_code"
    plugins = {item["id"]: item for item in response.data["plugins"]}
    assert plugins["computer-use"]["enabled"] is True
    assert plugins["computer-use"]["installed"] is True
    assert plugins["computer-use"]["plugin_id"] == "openbase-computer-use"
    assert response.data["restart_required"] is False


def test_claude_plugin_settings_toggles_plugin_and_requests_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=claude-code\n", encoding="utf-8")
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    state = {"enabled": False}
    calls: list[bool] = []

    def fake_set_enabled(enabled: bool) -> bool:
        calls.append(enabled)
        changed = state["enabled"] != enabled
        state["enabled"] = enabled
        return changed

    monkeypatch.setattr(
        backend_settings.claude_plugins,
        "computer_use_enabled",
        lambda: state["enabled"],
    )
    monkeypatch.setattr(
        backend_settings.claude_plugins,
        "set_computer_use_enabled",
        fake_set_enabled,
    )

    response = backend_settings.claude_plugin_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/coding-backend/claude-plugins/",
            {"plugin": "computer-use", "enabled": True},
        )
    )

    assert response.status_code == 200
    assert calls == [True]
    assert response.data["changed"] is True
    assert response.data["changed_plugin"] == "computer-use"
    assert response.data["restart_required"] is True
    plugins = {item["id"]: item for item in response.data["plugins"]}
    assert plugins["computer-use"]["enabled"] is True

    unchanged = backend_settings.claude_plugin_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/coding-backend/claude-plugins/",
            {"plugin": "computer-use", "enabled": True},
        )
    )
    assert unchanged.status_code == 200
    assert unchanged.data["changed"] is False
    assert unchanged.data["restart_required"] is False


def test_claude_plugin_settings_rejects_unknown_plugin() -> None:
    response = backend_settings.claude_plugin_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/coding-backend/claude-plugins/",
            {"plugin": "surprise", "enabled": True},
        )
    )

    assert response.status_code == 400
    assert "plugin" in response.data


def test_claude_plugin_settings_toggles_chrome_independently(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=claude-code\n", encoding="utf-8")
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    state = {"computer-use": True, "chrome": False}

    monkeypatch.setattr(
        backend_settings.claude_plugins,
        "computer_use_enabled",
        lambda: state["computer-use"],
    )
    monkeypatch.setattr(
        backend_settings.claude_plugins,
        "chrome_enabled",
        lambda: state["chrome"],
    )

    def fake_set_chrome(enabled: bool) -> bool:
        changed = state["chrome"] != enabled
        state["chrome"] = enabled
        return changed

    monkeypatch.setattr(
        backend_settings.claude_plugins, "set_chrome_enabled", fake_set_chrome
    )

    response = backend_settings.claude_plugin_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/coding-backend/claude-plugins/",
            {"plugin": "chrome", "enabled": True},
        )
    )

    assert response.status_code == 200
    assert response.data["changed"] is True
    assert response.data["changed_plugin"] == "chrome"
    plugins = {item["id"]: item for item in response.data["plugins"]}
    assert plugins["chrome"]["enabled"] is True
    assert plugins["chrome"]["plugin_id"] == "claude-in-chrome"
    assert plugins["computer-use"]["enabled"] is True


def test_urlconf_loads_with_all_settings_views() -> None:
    # Resolving through the root URLconf exercises the views.py re-export
    # chain; a missing re-export 500s every request in a real install.
    from django.urls import reverse

    assert reverse("coding-backend-claude-plugin-settings")
    assert reverse("coding-backend-codex-plugin-settings")


def test_coding_backend_settings_verifies_claude_login_on_save(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Saving the Claude backend runs a verified (probe-backed) status check."""
    env_file = tmp_path / ".env"
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        backend_settings,
        "verified_claude_auth_status",
        lambda: SimpleNamespace(
            logged_in=False, raw_output='{"loggedIn": false}', returncode=1
        ),
    )

    response = backend_settings.coding_backend_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/coding-backend/",
            {"backend": "claude_code"},
        )
    )

    assert response.status_code == 200
    assert response.data["claude_auth"]["logged_in"] is False
    assert response.data["claude_auth"]["command"] == "claude login"
    assert response.data["claude_auth"]["verified"] is True

def test_coding_backend_location_local_engages_both_engines(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=openbase_cloud\n", encoding="utf-8")
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        backend_settings,
        "verified_claude_auth_status",
        lambda: SimpleNamespace(
            logged_in=True, raw_output='{"loggedIn": true}', returncode=0
        ),
    )
    response = backend_settings.coding_backend_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/coding-backend/",
            {"location": "local"},
        )
    )

    assert response.status_code == 200
    content = env_file.read_text(encoding="utf-8")
    assert "OPENBASE_CODING_BACKENDS=codex,claude_code" in content
    assert response.data["location"] == "local"


def test_coding_backend_location_cloud_sets_openbase_cloud_primary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=codex\n", encoding="utf-8")
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        backend_settings,
        "verified_claude_auth_status",
        lambda: SimpleNamespace(
            logged_in=True, raw_output='{"loggedIn": true}', returncode=0
        ),
    )

    response = backend_settings.coding_backend_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/coding-backend/",
            {"location": "cloud"},
        )
    )

    assert response.status_code == 200
    content = env_file.read_text(encoding="utf-8")
    assert "OPENBASE_CODING_BACKEND=openbase_cloud" in content
    assert "OPENBASE_CODING_BACKENDS=openbase_cloud,codex" in content
    assert response.data["location"] == "cloud"
