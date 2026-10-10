from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from click.testing import CliRunner

setup_cli = importlib.import_module("openbase_coder_cli.cli.setup")


@pytest.mark.parametrize("developer_install", [True, False])
@pytest.mark.parametrize("include_hooks", [True, False])
@pytest.mark.parametrize("shared_mcp", [True, False])
def test_interactive_setup_summarizes_developer_agent_configuration(
    monkeypatch,
    tmp_path: Path,
    include_hooks: bool,
    shared_mcp: bool,
    developer_install: bool,
) -> None:
    monkeypatch.setattr(setup_cli, "is_supported", lambda: True)
    monkeypatch.setattr(setup_cli, "_run_setup_phases", lambda *_a, **_kw: True)
    monkeypatch.setattr(setup_cli, "compute_cli_configured", lambda: True)
    monkeypatch.setattr(
        setup_cli,
        "current_runtime_package",
        lambda: None if developer_install else object(),
    )
    monkeypatch.setattr(
        setup_cli, "_interactive_cloud_login_and_checks", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(setup_cli, "_print_app_download_qr", lambda: None)
    monkeypatch.setattr(
        setup_cli.SystemSetupSnapshot,
        "capture",
        lambda _path: setup_cli.SystemSetupSnapshot(),
    )

    result = CliRunner().invoke(
        setup_cli.setup,
        [
            "--interactive",
            "--env-file",
            str(tmp_path / ".env"),
            "--backend",
            "codex",
            "--audio-provider",
            "openbase-cloud",
            "--tailnet-provider",
            "tailscale",
            "--include-default-hooks"
            if include_hooks
            else "--no-include-default-hooks",
            "--shared-super-agents-mcp"
            if shared_mcp
            else "--no-shared-super-agents-mcp",
        ],
    )

    assert result.exit_code == 0, result.output
    if not developer_install:
        assert "Your Codex and Claude Code setup" not in result.output
        return
    paragraph = next(
        line
        for line in result.output.splitlines()
        if line.startswith("ℹ️  Your Codex and Claude Code setup")
    )
    assert paragraph.startswith("ℹ️  Your Codex and Claude Code setup")
    assert "session profiles" in paragraph
    assert "bundled Openbase skills" in paragraph
    assert (
        "ordinary sessions receive their Agent-Thread-Id" in paragraph
    ) == include_hooks
    assert ("hooks are scoped to its session profiles" in paragraph) == (
        not include_hooks
    )
    assert ("registration is enabled" in paragraph) == shared_mcp
    assert ("configurations is disabled" in paragraph) == (not shared_mcp)
    assert paragraph.endswith("process to load these settings.")
    assert result.output.rstrip().split("\n")[-1].startswith("ℹ️ System changes:")
