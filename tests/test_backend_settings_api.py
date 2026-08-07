from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    backend_settings,
    model_settings,
)


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
    assert [option["id"] for option in response.data["options"]] == [
        "fable",
        "opus",
        "sonnet",
        "haiku",
    ]


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
    assert [option["id"] for option in response.data["options"]] == [
        "openbase-claude",
        "fable",
    ]


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
            {"role": "dispatcher", "model": "gpt-dispatcher"},
        )
    )

    assert response.status_code == 200
    assert response.data["models"]["dispatcher"] == "gpt-dispatcher"
    assert response.data["restart_required"] is True


def test_coding_backend_settings_persists_claude_code_selection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        backend_settings,
        "claude_auth_status",
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
    assert response.data["claude_auth"]["command"] == "openbase-coder claude sync-state"
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
        "sync_normal_claude_state",
        lambda: SimpleNamespace(
            state_updated=True, message="Synced normal Claude Code state into Openbase."
        ),
    )
    monkeypatch.setattr(backend_settings, "copy_normal_claude_keychain", lambda: True)
    statuses = iter(
        [
            SimpleNamespace(
                logged_in=False, raw_output='{"loggedIn": false}', returncode=1
            ),
            SimpleNamespace(
                logged_in=True, raw_output='{"loggedIn": true}', returncode=0
            ),
        ]
    )
    monkeypatch.setattr(backend_settings, "claude_auth_status", lambda: next(statuses))

    response = backend_settings.claude_auth_settings(
        _authenticated_request("POST", "/api/settings/coding-backend/claude-auth/")
    )

    assert response.status_code == 200
    assert response.data["command"] == "openbase-coder claude sync-state"
    assert response.data["logged_in"] is True
    assert response.data["state_updated"] is True
    assert response.data["keychain_copied"] is True


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


def test_coding_backend_settings_heals_dead_claude_login_on_save(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    monkeypatch.setattr(backend_settings, "DEFAULT_ENV_FILE_PATH", env_file)
    statuses = iter(
        [
            SimpleNamespace(
                logged_in=False, raw_output='{"loggedIn": false}', returncode=1
            ),
            SimpleNamespace(
                logged_in=True, raw_output='{"loggedIn": true}', returncode=0
            ),
        ]
    )
    monkeypatch.setattr(backend_settings, "claude_auth_status", lambda: next(statuses))
    synced: list[str] = []
    monkeypatch.setattr(
        backend_settings,
        "sync_normal_claude_state",
        lambda: (
            synced.append("state")
            or SimpleNamespace(state_updated=True, message="synced")
        ),
    )
    monkeypatch.setattr(
        backend_settings,
        "copy_normal_claude_keychain",
        lambda: synced.append("keychain") or True,
    )

    response = backend_settings.coding_backend_settings(
        _authenticated_request(
            "PUT",
            "/api/settings/coding-backend/",
            {"backend": "claude_code"},
        )
    )

    assert response.status_code == 200
    assert synced == ["state", "keychain"]
    assert response.data["claude_auth"]["logged_in"] is True
    assert response.data["claude_auth"]["keychain_copied"] is True
    assert response.data["claude_auth"]["state_updated"] is True
