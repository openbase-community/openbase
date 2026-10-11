from openbase_coder_cli.services import selection


def test_configured_coding_backends_reads_env_file(tmp_path, monkeypatch):
    """A shell without the env loaded must select launchd's backends too."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENBASE_CODING_BACKEND=claude_code\n"
        "OPENBASE_CODING_BACKENDS=codex,claude_code\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENBASE_CODING_BACKENDS", raising=False)
    monkeypatch.setattr(selection, "configured_env_file_path", lambda: env_file)

    assert selection.configured_coding_backends() == ["claude_code", "codex"]
    names = {service.name for service in selection.configured_default_services()}
    assert {"codex-app-server", "codex-app-server-dispatcher"} <= names


def test_process_environment_overrides_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENBASE_CODING_BACKEND=claude_code\n"
        "OPENBASE_CODING_BACKENDS=codex,claude_code\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENBASE_CODING_BACKENDS", "claude_code")
    monkeypatch.setattr(selection, "configured_env_file_path", lambda: env_file)

    assert selection.configured_coding_backends() == ["claude_code"]
