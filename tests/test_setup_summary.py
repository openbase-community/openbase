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
    assert result.output.rstrip().split("\n")[-1].startswith("ℹ️  System changes:")


def test_deferred_notes_file_holds_the_info_summaries_for_the_wrapper(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup_cli, "is_supported", lambda: True)
    monkeypatch.setattr(setup_cli, "_run_setup_phases", lambda *_a, **_kw: True)
    monkeypatch.setattr(setup_cli, "compute_cli_configured", lambda: True)
    monkeypatch.setattr(setup_cli, "current_runtime_package", lambda: None)
    monkeypatch.setattr(
        setup_cli, "_interactive_cloud_login_and_checks", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        setup_cli.SystemSetupSnapshot,
        "capture",
        lambda _path: setup_cli.SystemSetupSnapshot(),
    )
    notes_file = tmp_path / "notes.txt"

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
            "--defer-app-qr",
            "--deferred-notes-file",
            str(notes_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "ℹ️" not in result.output
    notes = [line for line in notes_file.read_text().splitlines() if line]
    assert notes[0].startswith("ℹ️  Your Codex and Claude Code setup")
    assert notes[1].startswith("ℹ️  System changes:")


@pytest.mark.parametrize("defer_app_qr", [True, False])
def test_interactive_setup_defers_app_qr_to_the_workspace_wrapper(
    monkeypatch, tmp_path: Path, defer_app_qr: bool
) -> None:
    monkeypatch.setattr(setup_cli, "is_supported", lambda: True)
    monkeypatch.setattr(setup_cli, "_run_setup_phases", lambda *_a, **_kw: True)
    monkeypatch.setattr(setup_cli, "compute_cli_configured", lambda: True)
    monkeypatch.setattr(setup_cli, "current_runtime_package", lambda: object())
    monkeypatch.setattr(
        setup_cli, "_interactive_cloud_login_and_checks", lambda *_a, **_kw: None
    )
    printed: list[bool] = []
    monkeypatch.setattr(
        setup_cli, "_print_app_download_qr", lambda: printed.append(True)
    )

    args = [
        "--interactive",
        "--env-file",
        str(tmp_path / ".env"),
        "--backend",
        "codex",
        "--audio-provider",
        "openbase-cloud",
        "--tailnet-provider",
        "tailscale",
    ]
    if defer_app_qr:
        args.append("--defer-app-qr")
    result = CliRunner().invoke(setup_cli.setup, args)

    assert result.exit_code == 0, result.output
    assert printed == ([] if defer_app_qr else [True])


def test_app_download_qr_command_prints_the_downloads_url() -> None:
    from openbase_coder_cli.cli import main

    result = CliRunner().invoke(main, ["app-download-qr"])

    assert result.exit_code == 0, result.output
    assert "https://openbase.cloud/downloads.html" in result.output
