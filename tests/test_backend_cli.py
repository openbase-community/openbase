from __future__ import annotations

from click.testing import CliRunner

from openbase_coder_cli.cli import main


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
    config = (tmp_path / "nested" / "codex_home" / "config.toml").read_text(
        encoding="utf-8"
    )
    assert 'model = "openbase-codex"' in config
    assert 'model_provider = "openbase_cloud"' in config
    assert "[model_providers.openbase_cloud]" in config
    assert 'env_key = "OPENBASE_CLOUD_CODEX_API_KEY"' in config


def test_backend_use_accepts_spoken_openbase_cloud_alias(tmp_path) -> None:
    env_file = tmp_path / ".env"

    result = CliRunner().invoke(
        main, ["backend", "use", "openbase", "cloud", "--env-file", str(env_file)]
    )

    assert result.exit_code == 0
    assert "Backend set to openbase_cloud" in result.output
    assert "OPENBASE_CODING_BACKEND=openbase_cloud" in env_file.read_text(
        encoding="utf-8"
    )


def test_backend_use_codex_removes_openbase_cloud_provider(tmp_path) -> None:
    env_file = tmp_path / ".env"
    config_path = tmp_path / "codex_home" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                'model = "openbase-codex"',
                'model_provider = "openbase_cloud"',
                "",
                "[model_providers.openbase_cloud]",
                'name = "Openbase Cloud"',
                'base_url = "https://app.openbase.cloud/api/openbase/llm/openai/v1"',
                'env_key = "OPENBASE_CLOUD_CODEX_API_KEY"',
                'wire_api = "responses"',
                "",
                "[mcp_servers.super-agents]",
                'command = "super-agents-mcp"',
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main, ["backend", "use", "codex", "--env-file", str(env_file)]
    )

    assert result.exit_code == 0
    config = config_path.read_text(encoding="utf-8")
    assert 'model = "gpt-5.5"' in config
    assert "model_provider" not in config
    assert "[model_providers.openbase_cloud]" not in config
    assert '[mcp_servers.super-agents]\ncommand = "super-agents-mcp"' in config


def test_internal_openbase_cloud_codex_uses_configured_web_backend(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENBASE_CODER_CLI_WEB_BACKEND_URL=http://localhost:8000\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        ["backend", "use", "openbase-cloud-codex", "--env-file", str(env_file)],
    )

    assert result.exit_code == 0
    config = (tmp_path / "codex_home" / "config.toml").read_text(encoding="utf-8")
    assert 'base_url = "http://localhost:8000/api/openbase/llm/openai/v1"' in config


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
