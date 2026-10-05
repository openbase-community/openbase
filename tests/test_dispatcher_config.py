from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openbase_coder_cli import claude_auth, dispatcher_config
from openbase_coder_cli.services import selection


def _install_with(
    monkeypatch, *, backends: tuple[str, ...], claude_logged_in: bool
) -> None:
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
    # Availability checks must never read the real install or run ``claude``.
    _install_with(monkeypatch, backends=("codex", "claude_code"), claude_logged_in=True)


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
        "haiku",
        "sonnet",
        "opus",
        "fable",
    ]
    assert options[0]["is_default"] is True
    assert "Trial accounts run Claude Haiku" in options[1]["description"]


def test_codex_model_options_cover_runtime_catalog() -> None:
    from super_agents.backend_config import CODEX_BACKEND, MODEL_CATALOG

    codex_ids = [option["id"] for option in dispatcher_config.CODEX_MODEL_OPTIONS]

    assert codex_ids == ["gpt-5.5", "gpt-5", "sol", "astra"]
    # Every selectable Codex option must be one the runtime routes to Codex.
    assert set(codex_ids) <= MODEL_CATALOG[CODEX_BACKEND]
    for model in codex_ids:
        assert dispatcher_config.model_engine(model) == dispatcher_config.CODEX_ENGINE
        assert dispatcher_config.is_known_combined_model(
            model, dispatcher_config.LOCATION_LOCAL
        )
        assert not dispatcher_config.is_known_combined_model(
            model, dispatcher_config.LOCATION_CLOUD
        )
        assert (
            dispatcher_config.identity_for_model(model, dispatcher_config.LOCATION_LOCAL)
            == "codex"
        )
        assert (
            dispatcher_config.identity_for_model(model, dispatcher_config.LOCATION_CLOUD)
            == "openbase_cloud_codex"
        )


def test_engine_unavailable_reason_codex_needs_codex_services(monkeypatch) -> None:
    _install_with(monkeypatch, backends=("claude_code",), claude_logged_in=True)

    assert (
        dispatcher_config.engine_unavailable_reason(
            dispatcher_config.CODEX_ENGINE, dispatcher_config.LOCATION_LOCAL
        )
        == dispatcher_config.CODEX_NOT_INSTALLED_REASON
    )
    assert (
        dispatcher_config.engine_unavailable_reason(
            dispatcher_config.CLAUDE_ENGINE, dispatcher_config.LOCATION_LOCAL
        )
        is None
    )
    options = {
        option["id"]: option
        for option in dispatcher_config.combined_model_options(
            dispatcher_config.LOCATION_LOCAL
        )
    }
    assert options["gpt-5.5"]["available"] is False
    assert (
        options["gpt-5.5"]["unavailable_reason"]
        == dispatcher_config.CODEX_NOT_INSTALLED_REASON
    )
    assert options["fable"]["available"] is True
    assert options["fable"]["unavailable_reason"] is None
    assert not dispatcher_config.is_known_combined_model(
        "gpt-5.5", dispatcher_config.LOCATION_LOCAL
    )


def test_engine_unavailable_reason_claude_needs_login(monkeypatch) -> None:
    _install_with(monkeypatch, backends=("codex",), claude_logged_in=False)

    assert (
        dispatcher_config.engine_unavailable_reason(
            dispatcher_config.CLAUDE_ENGINE, dispatcher_config.LOCATION_LOCAL
        )
        == dispatcher_config.CLAUDE_NOT_LOGGED_IN_REASON
    )
    options = {
        option["id"]: option
        for option in dispatcher_config.combined_model_options(
            dispatcher_config.LOCATION_LOCAL
        )
    }
    assert options["fable"]["available"] is False
    assert (
        options["fable"]["unavailable_reason"]
        == dispatcher_config.CLAUDE_NOT_LOGGED_IN_REASON
    )
    assert options["gpt-5.5"]["available"] is True
    assert not dispatcher_config.is_known_combined_model(
        "fable", dispatcher_config.LOCATION_LOCAL
    )


def test_engine_unavailable_reason_on_cloud_skips_local_checks(monkeypatch) -> None:
    """On Openbase Cloud, Codex is read-only and Claude runs through the
    proxy, so neither local check (services, ``claude`` login) is consulted."""

    def _never(*args, **kwargs):
        raise AssertionError("local availability check consulted on cloud")

    monkeypatch.setattr(selection, "service_supports_configured_backends", _never)
    monkeypatch.setattr(claude_auth, "claude_auth_status", _never)

    assert (
        dispatcher_config.engine_unavailable_reason(
            dispatcher_config.CODEX_ENGINE, dispatcher_config.LOCATION_CLOUD
        )
        == dispatcher_config.CODEX_CLOUD_UNAVAILABLE_REASON
    )
    assert (
        dispatcher_config.engine_unavailable_reason(
            dispatcher_config.CLAUDE_ENGINE, dispatcher_config.LOCATION_CLOUD
        )
        is None
    )


def test_set_backend_model_writes_other_codex_models(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatcher-config.json"

    dispatcher_config.set_backend_model(
        "dispatcher", "sol", backend="codex", path=config_path
    )
    dispatcher_config.set_backend_model(
        "super_agents", "astra", backend="openbase_cloud_codex", path=config_path
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["backend_models"]["codex"]["dispatcher"] == "sol"
    assert payload["backend_models"]["openbase_cloud_codex"]["super_agents"] == "astra"
    assert payload["role_models"] == {"dispatcher": "sol", "super_agents": "astra"}


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


def test_default_setup_config_uses_haiku_for_openbase_cloud(
    tmp_path: Path, monkeypatch
) -> None:
    # A fresh install must NOT let openbase_cloud inherit the personal
    # claude_code "opus" default or rely on a hidden Sonnet-to-Haiku reroute.
    from openbase_coder_cli.cli.setup.dispatcher import (
        CODEX_HOME_DEFAULT_DISPATCHER_CONFIG,
    )

    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text(
        json.dumps(CODEX_HOME_DEFAULT_DISPATCHER_CONFIG), encoding="utf-8"
    )

    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "openbase_cloud")
    assert dispatcher_config.dispatcher_model(config_path) == "haiku"
    assert dispatcher_config.super_agents_model(config_path) == "haiku"

    # Personal claude_code login keeps opus (its own plan allows it).
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "claude_code")
    assert dispatcher_config.dispatcher_model(config_path) == "opus"
