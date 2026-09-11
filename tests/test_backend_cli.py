from __future__ import annotations

from click.testing import CliRunner

from openbase_coder_cli.cli import main
from openbase_coder_cli.codex_backend_config import codex_backend_profile_config


def test_backend_status_defaults_when_env_file_missing(tmp_path) -> None:
    env_file = tmp_path / ".env"
    result = CliRunner().invoke(
        main, ["backend", "status", "--env-file", str(env_file)]
    )

    assert result.exit_code == 0
    assert "Backend: codex" in result.output
    assert "missing" in result.output


def test_backend_use_writes_canonical_backend_and_preserves_file(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP_ME=1\nOPENBASE_CODEX_BACKEND=codex\n", encoding="utf-8")

    result = CliRunner().invoke(
        main, ["backend", "use", "claude-code", "--env-file", str(env_file)]
    )

    assert result.exit_code == 0
    assert "Backend set to claude_code" in result.output
    content = env_file.read_text(encoding="utf-8")
    assert "KEEP_ME=1" in content
    assert "OPENBASE_CODING_BACKEND=claude_code" in content
    assert "OPENBASE_CODEX_BACKEND=codex" in content


def test_backend_use_creates_env_file(tmp_path) -> None:
    env_file = tmp_path / "nested" / ".env"

    result = CliRunner().invoke(
        main, ["backend", "use", "openbase-cloud", "--env-file", str(env_file)]
    )

    assert result.exit_code == 0
    assert (
        env_file.read_text(encoding="utf-8")
        == "OPENBASE_CODING_BACKEND=openbase_cloud\n"
    )
    assert not (tmp_path / "nested" / "codex_home" / "config.toml").exists()


def test_backend_use_internal_openbase_cloud_codex_keeps_codex_proxy(tmp_path) -> None:
    env_file = tmp_path / "nested" / ".env"

    result = CliRunner().invoke(
        main,
        [
            "backend",
            "use",
            "openbase-cloud-codex",
            "--env-file",
            str(env_file),
        ],
    )

    assert result.exit_code == 0
    assert "OPENBASE_CODING_BACKEND=openbase_cloud_codex" in env_file.read_text(
        encoding="utf-8"
    )


def test_codex_backend_profile_config_for_openbase_cloud(monkeypatch) -> None:
    monkeypatch.setenv("OPENBASE_CLOUD_CODEX_MODEL", "cloud-test-model")
    config = codex_backend_profile_config("openbase_cloud_codex")
    assert config["model"] == "cloud-test-model"
    assert config["model_provider"] == "openbase_cloud"
    assert config["model_providers"]["openbase_cloud"] == {
        "name": "Openbase Cloud",
        "base_url": "https://app.openbase.cloud/api/openbase/llm/openai/v1",
        "env_key": "OPENBASE_CLOUD_CODEX_API_KEY",
        "wire_api": "responses",
    }


def test_codex_backend_profile_config_for_direct_codex() -> None:
    assert codex_backend_profile_config("codex") == {
        "model": "gpt-5.5",
        "model_provider": "openai",
    }


def test_codex_backend_profile_config_uses_configured_web_backend() -> None:
    config = codex_backend_profile_config(
        "openbase_cloud_codex", web_backend_url="http://localhost:8000"
    )

    assert config["model_providers"]["openbase_cloud"]["base_url"] == (
        "http://localhost:8000/api/openbase/llm/openai/v1"
    )


def test_backend_status_reports_unsupported_value(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=surprise\n", encoding="utf-8")

    result = CliRunner().invoke(
        main, ["backend", "status", "--env-file", str(env_file)]
    )

    assert result.exit_code == 0
    assert "Backend: unsupported:surprise" in result.output


def test_backend_status_reads_legacy_env_key(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=claude-code\n", encoding="utf-8")

    result = CliRunner().invoke(
        main, ["backend", "status", "--env-file", str(env_file)]
    )

    assert result.exit_code == 0
    assert "Backend: claude_code" in result.output


def test_backend_list_shows_supported_values() -> None:
    result = CliRunner().invoke(main, ["backend", "list"])

    assert result.exit_code == 0
    assert "codex (default)" in result.output
    assert "openbase_cloud" in result.output
    assert "claude_code" in result.output
    assert "openbase_cloud_codex" not in result.output
    assert "claude-tui" not in result.output
    assert "proxy" not in result.output


def test_backend_use_rejects_unsupported_backend(tmp_path) -> None:
    env_file = tmp_path / ".env"

    result = CliRunner().invoke(
        main, ["backend", "use", "claude-code-proxy", "--env-file", str(env_file)]
    )

    assert result.exit_code != 0
    assert "Unsupported backend" in result.output
    assert not env_file.exists()
