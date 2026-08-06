from __future__ import annotations

import importlib

import click
from click.testing import CliRunner

codex_home_instructions = importlib.import_module(
    "openbase_coder_cli.codex_home_instructions"
)
main_cli = importlib.import_module("openbase_coder_cli.cli")


def test_ensure_openbase_agents_md_preserves_user_h2_sections(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "instructions" / "AGENTS.md"
    codex_home = tmp_path / "codex_home"
    agents = codex_home / "AGENTS.md"
    source.parent.mkdir(parents=True)
    agents.parent.mkdir(parents=True)
    source.write_text("- New repo rule\n", encoding="utf-8")
    agents.write_text(
        "# Personal instructions\n\n"
        "- Keep this custom top-level note.\n\n"
        "## Openbase Coder Instructions\n\n"
        "- Old generated note.\n"
        "- Old repo rule.\n\n"
        "## My Project Notes\n\n"
        "- Keep this project note.\n",
        encoding="utf-8",
    )

    changed = codex_home_instructions.ensure_openbase_agents_md(
        workspace,
        codex_home_dir=codex_home,
        include_normal_codex_agents=False,
    )

    assert changed is True
    assert agents.read_text(encoding="utf-8") == (
        "## Openbase Coder Instructions\n\n"
        f"- These instructions are auto generated from {source}.\n\n"
        "- New repo rule\n"
    )


def test_ensure_openbase_agents_md_demotes_generated_h2s(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "instructions" / "AGENTS.md"
    codex_home = tmp_path / "codex_home"
    source.parent.mkdir(parents=True)
    source.write_text("## Repo Section\n\n- Standard rule\n", encoding="utf-8")

    codex_home_instructions.ensure_openbase_agents_md(
        workspace,
        codex_home_dir=codex_home,
        include_normal_codex_agents=False,
    )

    content = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Repo Section" not in content.splitlines()
    assert "### Repo Section" in content.splitlines()


def test_ensure_openbase_agents_md_interpolates_confirmation_phrase(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "instructions" / "AGENTS.md"
    codex_home = tmp_path / "codex_home"
    source.parent.mkdir(parents=True)
    source.write_text(
        '- Require "${dangerous_confirmation_phrase}" before publishing.\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "openbase_coder_cli.instruction_templates.get_dangerous_confirmation_phrase",
        lambda: "ship it",
    )

    codex_home_instructions.ensure_openbase_agents_md(
        workspace,
        codex_home_dir=codex_home,
        include_normal_codex_agents=False,
    )

    content = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert '"ship it"' in content
    assert "${dangerous_confirmation_phrase}" not in content


def test_ensure_openbase_agents_md_interpolates_user_address_name(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "instructions" / "AGENTS.md"
    codex_home = tmp_path / "codex_home"
    source.parent.mkdir(parents=True)
    source.write_text(
        "- Tell ${user_address_name} the setup needs attention.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "openbase_coder_cli.instruction_templates.get_user_address_name",
        lambda: "Sam",
    )

    codex_home_instructions.ensure_openbase_agents_md(
        workspace,
        codex_home_dir=codex_home,
        include_normal_codex_agents=False,
    )

    content = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert "Tell Sam" in content
    assert "${user_address_name}" not in content


def test_ensure_rendered_instruction_file_updates_managed_template(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "instructions" / "SUPER_AGENT_INSTRUCTIONS.md"
    target = tmp_path / "openbase" / "instructions" / "SUPER_AGENT_INSTRUCTIONS.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        'Requires "${dangerous_confirmation_phrase}" first.\n',
        encoding="utf-8",
    )
    target.parent.mkdir(parents=True)
    target.write_text('Requires "yes, proceed" first.\n', encoding="utf-8")
    monkeypatch.setattr(
        "openbase_coder_cli.instruction_templates.get_dangerous_confirmation_phrase",
        lambda: "ship it",
    )

    changed = codex_home_instructions.ensure_rendered_instruction_file(
        source,
        target,
        document_label="Super Agent instructions",
    )

    assert changed is True
    assert target.read_text(encoding="utf-8") == (
        f"<!-- Generated from {source}; edit the source template instead. -->\n\n"
        'Requires "ship it" first.\n'
    )


def test_ensure_rendered_instruction_file_refreshes_generated_files_on_template_change(
    tmp_path,
) -> None:
    """A generated-from header marks the file machine-managed: template
    updates must propagate instead of being mistaken for user edits."""
    source = tmp_path / "instructions" / "DISPATCHER_INSTRUCTIONS.md"
    target = tmp_path / "openbase" / "instructions" / "DISPATCHER_INSTRUCTIONS.md"
    source.parent.mkdir(parents=True)
    source.write_text("New dispatcher rules.\n", encoding="utf-8")
    target.parent.mkdir(parents=True)
    target.write_text(
        "<!-- Generated from /old/path/DISPATCHER_INSTRUCTIONS.md; "
        "edit the source template instead. -->\n\nOld dispatcher rules.\n",
        encoding="utf-8",
    )

    changed = codex_home_instructions.ensure_rendered_instruction_file(
        source,
        target,
        document_label="Dispatcher instructions",
    )

    assert changed is True
    assert "New dispatcher rules." in target.read_text(encoding="utf-8")


def test_ensure_rendered_instruction_file_preserves_unmarked_user_files(
    tmp_path,
) -> None:
    source = tmp_path / "instructions" / "DISPATCHER_INSTRUCTIONS.md"
    target = tmp_path / "openbase" / "instructions" / "DISPATCHER_INSTRUCTIONS.md"
    source.parent.mkdir(parents=True)
    source.write_text("New dispatcher rules.\n", encoding="utf-8")
    target.parent.mkdir(parents=True)
    target.write_text("My hand-written dispatcher rules.\n", encoding="utf-8")

    changed = codex_home_instructions.ensure_rendered_instruction_file(
        source,
        target,
        document_label="Dispatcher instructions",
    )

    assert changed is False
    assert "My hand-written" in target.read_text(encoding="utf-8")


def test_ensure_rendered_instruction_file_records_template_source(
    tmp_path,
) -> None:
    source = tmp_path / "instructions" / "VOICE_INSTRUCTIONS.md"
    target = tmp_path / "openbase" / "instructions" / "VOICE_INSTRUCTIONS.md"
    source.parent.mkdir(parents=True)
    source.write_text("Voice instructions.\n", encoding="utf-8")

    changed = codex_home_instructions.ensure_rendered_instruction_file(
        source,
        target,
        document_label="Voice instructions",
    )

    assert changed is True
    assert target.read_text(encoding="utf-8") == (
        f"<!-- Generated from {source}; edit the source template instead. -->\n\n"
        "Voice instructions.\n"
    )


def test_refresh_openbase_agents_md_from_installation_uses_saved_workspace(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "instructions" / "AGENTS.md"
    codex_home = tmp_path / "codex_home"
    source.parent.mkdir(parents=True)
    source.write_text("- Standard rule\n", encoding="utf-8")

    class FakeInstallationConfig:
        @classmethod
        def exists(cls) -> bool:
            return True

        @classmethod
        def load(cls):
            return cls()

        workspace_path = str(workspace)

    monkeypatch.setattr(
        codex_home_instructions,
        "InstallationConfig",
        FakeInstallationConfig,
    )
    monkeypatch.setattr(codex_home_instructions, "CODEX_HOME_DIR", codex_home)
    monkeypatch.setattr(
        codex_home_instructions,
        "NORMAL_CODEX_AGENTS_MD_PATH",
        tmp_path / "missing-normal" / "AGENTS.md",
    )

    assert codex_home_instructions.refresh_openbase_agents_md_from_installation()
    assert (codex_home / "AGENTS.md").read_text(encoding="utf-8") == (
        "## Openbase Coder Instructions\n\n"
        f"- These instructions are auto generated from {source}.\n\n"
        "- Standard rule\n"
    )


def test_ensure_openbase_agents_md_can_include_normal_codex_agents(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "instructions" / "AGENTS.md"
    codex_home = tmp_path / "codex_home"
    normal_agents = tmp_path / "normal" / "AGENTS.md"
    source.parent.mkdir(parents=True)
    normal_agents.parent.mkdir(parents=True)
    source.write_text("- Openbase rule\n", encoding="utf-8")
    normal_agents.write_text("- Personal rule\n", encoding="utf-8")
    monkeypatch.setattr(
        codex_home_instructions,
        "NORMAL_CODEX_AGENTS_MD_PATH",
        normal_agents,
    )

    codex_home_instructions.ensure_openbase_agents_md(
        workspace,
        codex_home_dir=codex_home,
        include_normal_codex_agents=True,
    )

    assert (codex_home / "AGENTS.md").read_text(encoding="utf-8") == (
        "## Non-Openbase Instructions\n\n"
        f"- These instructions are included from {normal_agents}.\n\n"
        "- Personal rule\n\n"
        "## Openbase Coder Instructions\n\n"
        f"- These instructions are auto generated from {source}.\n\n"
        "- Openbase rule\n"
    )


def test_cli_launch_refreshes_openbase_agents_md(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        main_cli,
        "refresh_openbase_agents_md_from_installation",
        lambda: calls.append("refresh"),
    )

    @click.command("noop-refresh-test")
    def noop_refresh_test() -> None:
        click.echo("ok")

    main_cli.main.add_command(noop_refresh_test)
    try:
        result = CliRunner().invoke(main_cli.main, ["noop-refresh-test"])
    finally:
        del main_cli.main.commands["noop-refresh-test"]

    assert result.exit_code == 0
    assert calls == ["refresh"]


def test_ensure_rendered_instruction_file_standalone_overwrites_user_edits(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(codex_home_instructions, "is_standalone_runtime", lambda: True)
    source = tmp_path / "instructions" / "DISPATCHER_INSTRUCTIONS.md"
    source.parent.mkdir(parents=True)
    source.write_text("- Packaged rule\n", encoding="utf-8")
    target = tmp_path / "rendered" / "DISPATCHER_INSTRUCTIONS.md"
    target.parent.mkdir(parents=True)
    target.write_text("- A local edit that must not stick\n", encoding="utf-8")

    changed = codex_home_instructions.ensure_rendered_instruction_file(
        source, target, document_label="dispatcher instructions"
    )

    assert changed
    content = target.read_text(encoding="utf-8")
    assert "Packaged rule" in content
    assert "must not stick" not in content
    # Read-only so the file does not invite editing; re-render still works.
    assert (target.stat().st_mode & 0o222) == 0
    changed_again = codex_home_instructions.ensure_rendered_instruction_file(
        source, target, document_label="dispatcher instructions"
    )
    assert not changed_again
    source.write_text("- Packaged rule v2\n", encoding="utf-8")
    assert codex_home_instructions.ensure_rendered_instruction_file(
        source, target, document_label="dispatcher instructions"
    )
    assert "Packaged rule v2" in target.read_text(encoding="utf-8")
