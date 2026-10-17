"""Sandbox agents are told what a cloud workspace keeps across an upgrade.

Only the data volume survives an in-place image upgrade of a cloud workspace;
the rest of the home folder is image-layer, and a user's API key saved under ~
was gone after the 2026-10-16 production upgrades. The rendered instructions
carry one bullet about it on a cloud workspace and nothing elsewhere, and the
container entrypoint points Super Agents started without a project at the
durable projects dir.
"""

from __future__ import annotations

from pathlib import Path

from openbase_coder_cli import host_kind, instruction_templates
from openbase_coder_cli.host_kind import (
    CLOUD_WORKSPACE_PERSISTENCE_NOTE,
    HOST_KIND_CLOUD_WORKSPACE,
    HOST_KIND_LINUX,
    HOST_KIND_MAC,
    cloud_workspace_note,
    host_note,
)

ROOT = Path(__file__).parents[1]


def test_note_names_the_projects_folder_and_what_is_reset() -> None:
    assert "/data/workspace" in CLOUD_WORKSPACE_PERSISTENCE_NOTE
    assert "OPENBASE_CODER_PROJECTS_DIR" in CLOUD_WORKSPACE_PERSISTENCE_NOTE
    assert "reset" in CLOUD_WORKSPACE_PERSISTENCE_NOTE
    for kept in ("~/.codex", "~/.claude", "~/.config/gh", "~/.super-agents"):
        assert kept in CLOUD_WORKSPACE_PERSISTENCE_NOTE


def test_bullet_renders_only_on_a_cloud_workspace() -> None:
    assert (
        cloud_workspace_note(HOST_KIND_CLOUD_WORKSPACE)
        == f"- {CLOUD_WORKSPACE_PERSISTENCE_NOTE}"
    )
    assert cloud_workspace_note(HOST_KIND_MAC) == ""
    assert cloud_workspace_note(HOST_KIND_LINUX) == ""


def test_dispatcher_host_note_carries_it_only_on_a_cloud_workspace() -> None:
    assert CLOUD_WORKSPACE_PERSISTENCE_NOTE in host_note(HOST_KIND_CLOUD_WORKSPACE)
    assert CLOUD_WORKSPACE_PERSISTENCE_NOTE not in host_note(HOST_KIND_MAC)


def test_template_token_renders_per_host(monkeypatch) -> None:
    monkeypatch.setattr(
        instruction_templates, "get_dangerous_confirmation_phrase", lambda: "go"
    )
    monkeypatch.setattr(instruction_templates, "get_user_address_name", lambda: "you")
    template = "- projects\n${cloud_workspace_note}\n- reports\n"

    monkeypatch.setattr(host_kind, "host_kind", lambda: HOST_KIND_CLOUD_WORKSPACE)
    rendered = instruction_templates.render_instruction_template(template)
    assert rendered == f"- projects\n- {CLOUD_WORKSPACE_PERSISTENCE_NOTE}\n- reports\n"
    assert instruction_templates.text_matches_instruction_template(rendered, template)

    monkeypatch.setattr(host_kind, "host_kind", lambda: HOST_KIND_MAC)
    rendered = instruction_templates.render_instruction_template(template)
    assert rendered == "- projects\n\n- reports\n"
    assert instruction_templates.text_matches_instruction_template(rendered, template)


def test_entrypoint_points_super_agents_at_the_durable_projects_dir() -> None:
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text()
    projects = entrypoint.index('export OPENBASE_CODER_PROJECTS_DIR="$PROJECTS_DIR"')
    default_cwd = entrypoint.index(
        'export SUPER_AGENTS_DEFAULT_CWD="${SUPER_AGENTS_DEFAULT_CWD:-$PROJECTS_DIR}"'
    )
    # Inside the Maritime block, right after the projects dir is settled, and
    # before any service starts.
    assert (
        projects
        < default_cwd
        < entrypoint.index('start_supervised "$name" bash "$wrapper"')
    )
    maritime_block = entrypoint.index(
        'if [ "${OPENBASE_CODER_RUNTIME:-}" = "maritime" ]; then\n    MARITIME_MODE=1'
    )
    assert maritime_block < default_cwd
