from __future__ import annotations

import json
from pathlib import Path

from openbase_coder_cli import dispatcher_config


def test_backend_model_uses_env_backend(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text(
        json.dumps(
            {
                "backend_models": {
                    "codex": {"dispatcher": "gpt-5.5", "super_agents": "gpt-5.5"},
                    "claude_code": {
                        "dispatcher": "sonnet",
                        "super_agents": "opus",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "claude_code")

    assert dispatcher_config.dispatcher_model(config_path) == "sonnet"
    assert dispatcher_config.super_agents_model(config_path) == "opus"


def test_claude_model_options_include_fable_alias(monkeypatch) -> None:
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "claude_code")

    options = dispatcher_config.model_options_for_backend()

    assert options[0]["id"] == "fable"
    assert dispatcher_config.is_known_backend_model("fable")


def test_openbase_cloud_model_options_include_fable(monkeypatch) -> None:
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "openbase_cloud")

    options = dispatcher_config.model_options_for_backend()

    assert [option["id"] for option in options] == [
        "sonnet",
        "opus",
        "fable",
        "haiku",
    ]


def test_backend_model_uses_env_file_backend(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    config_path = tmp_path / "dispatcher-config.json"
    env_file.write_text("OPENBASE_CODING_BACKEND=openbase_cloud\n", encoding="utf-8")
    config_path.write_text(
        json.dumps(
            {
                "backend_models": {
                    "codex": {"super_agents": "gpt-5.5"},
                    "openbase_cloud": {"super_agents": "openbase-claude"},
                },
                "super_agents_model": "legacy-model",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENBASE_CODING_BACKEND", raising=False)
    monkeypatch.setattr(dispatcher_config, "DEFAULT_ENV_FILE_PATH", env_file)

    assert dispatcher_config.super_agents_model(config_path) == "openbase-claude"


def test_super_agents_model_ignores_legacy_key(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text(
        json.dumps({"super_agents_model": "legacy-model"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "claude_code")

    assert dispatcher_config.super_agents_model(config_path) is None


def test_dispatcher_service_tier_uses_config_before_env(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    env_file = tmp_path / ".env"
    config_path.write_text(
        json.dumps({"dispatcher_service_tier": "standard"}),
        encoding="utf-8",
    )
    env_file.write_text("DISPATCHER_SERVICE_TIER=fast\n", encoding="utf-8")
    monkeypatch.setattr(dispatcher_config, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setenv("DISPATCHER_SERVICE_TIER", "fast")

    assert dispatcher_config.dispatcher_service_tier(config_path) == "standard"


def test_service_tier_scoped_defaults(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    monkeypatch.delenv("DISPATCHER_SERVICE_TIER", raising=False)
    monkeypatch.delenv("SUPER_AGENTS_SERVICE_TIER", raising=False)
    monkeypatch.setattr(dispatcher_config, "DEFAULT_ENV_FILE_PATH", env_file)
    missing = tmp_path / "missing.json"

    # Voice dispatch defaults fast; bulk super-agent work defaults standard.
    assert dispatcher_config.dispatcher_service_tier(missing) == "fast"
    assert dispatcher_config.super_agents_service_tier(missing) == "standard"


def test_set_service_tiers_persist_config(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatcher-config.json"

    dispatcher_config.set_dispatcher_service_tier("standard", config_path)
    dispatcher_config.set_super_agents_service_tier("fast", config_path)

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["dispatcher_service_tier"] == "standard"
    assert payload["super_agents_service_tier"] == "fast"


def test_default_setup_config_uses_sonnet_for_openbase_cloud(
    tmp_path: Path, monkeypatch
) -> None:
    # A fresh install must NOT let openbase_cloud inherit the personal
    # claude_code "opus" default: the Cloud proxy 403s opus for trial accounts.
    from openbase_coder_cli.cli.setup.dispatcher import (
        CODEX_HOME_DEFAULT_DISPATCHER_CONFIG,
    )

    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text(
        json.dumps(CODEX_HOME_DEFAULT_DISPATCHER_CONFIG), encoding="utf-8"
    )

    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "openbase_cloud")
    assert dispatcher_config.dispatcher_model(config_path) == "sonnet"
    assert dispatcher_config.super_agents_model(config_path) == "sonnet"

    # Personal claude_code login keeps opus (its own plan allows it).
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "claude_code")
    assert dispatcher_config.dispatcher_model(config_path) == "opus"
