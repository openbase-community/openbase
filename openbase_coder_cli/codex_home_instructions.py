from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from openbase_coder_cli.instruction_templates import (
    render_instruction_template,
    text_matches_instruction_template,
)
from openbase_coder_cli.paths import (
    CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH,
    CODEX_DISPATCHER_INSTRUCTIONS_PATH,
    CODEX_HOME_DIR,
    CODEX_SUPER_AGENT_INSTRUCTIONS_PATH,
    NORMAL_CODEX_AGENTS_MD_PATH,
    OPENBASE_CLAUDE_MD_PATH,
)
from openbase_coder_cli.runtime import (
    is_standalone_runtime,
    packaged_instructions_dir,
)
from openbase_coder_cli.services.installation import InstallationConfig

CODEX_HOME_DEFAULT_SOURCE_DIR = "instructions"
MANAGED_AGENTS_HEADING = "## Openbase Coder Instructions"
OPENBASE_DEFAULT_INSTRUCTION_FILES = (
    ("VOICE_INSTRUCTIONS.md", CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH),
    ("DISPATCHER_INSTRUCTIONS.md", CODEX_DISPATCHER_INSTRUCTIONS_PATH),
    ("SUPER_AGENT_INSTRUCTIONS.md", CODEX_SUPER_AGENT_INSTRUCTIONS_PATH),
)
GENERATED_INSTRUCTION_PREFIX = "<!-- Generated from "


def refresh_openbase_agents_md_from_installation() -> bool:
    """Refresh the editable Openbase Codex home AGENTS.md on CLI launch."""
    try:
        if not InstallationConfig.exists():
            return False
        config = InstallationConfig.load()
        source_root = _instruction_source_root(config.workspace_path)
        if source_root is None:
            return False
        return ensure_openbase_agents_md(source_root.parent)
    except Exception:
        return False


def refresh_openbase_instruction_files_from_installation(
    *,
    report: Callable[[str], None] | None = None,
) -> bool:
    """Refresh all managed instruction files that can contain console settings."""
    try:
        if not InstallationConfig.exists():
            return False
        config = InstallationConfig.load()
        source_root = _instruction_source_root(config.workspace_path)
        if source_root is None:
            return False

        changed = ensure_openbase_agents_md(
            source_root.parent,
            codex_home_dir=CODEX_HOME_DIR,
            report=report,
        )
        changed = ensure_openbase_claude_md_symlink(report=report) or changed
        for resource_name, target_path in OPENBASE_DEFAULT_INSTRUCTION_FILES:
            changed = (
                ensure_rendered_instruction_file(
                    source_root / resource_name,
                    target_path,
                    document_label=f"Openbase instruction {resource_name}",
                    report=report,
                )
                or changed
            )
        return changed
    except Exception:
        return False


def ensure_openbase_agents_md(
    workspace_dir: str | Path,
    *,
    codex_home_dir: Path | None = None,
    include_normal_codex_agents: bool | None = None,
    report: Callable[[str], None] | None = None,
) -> bool:
    """Maintain an editable AGENTS.md with a replaceable Openbase section."""
    return ensure_openbase_instruction_md(
        workspace_dir,
        target_path=(codex_home_dir or CODEX_HOME_DIR) / "AGENTS.md",
        document_label="Codex home AGENTS.md",
        include_normal_codex_agents=include_normal_codex_agents,
        report=report,
    )


def ensure_openbase_claude_md_symlink(
    *,
    report: Callable[[str], None] | None = None,
) -> bool:
    source_path = CODEX_HOME_DIR / "AGENTS.md"
    target_path = OPENBASE_CLAUDE_MD_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    relative_source = Path(os.path.relpath(source_path, target_path.parent))
    if target_path.is_symlink():
        if target_path.readlink() == relative_source:
            _report(report, f"Claude config CLAUDE.md already linked at {target_path}")
            return False
        target_path.unlink()
    elif target_path.exists():
        if target_path.is_dir():
            _report(
                report,
                f"Claude config CLAUDE.md already exists at {target_path}; leaving it unchanged.",
            )
            return False
        target_path.unlink()
    target_path.symlink_to(relative_source)
    _report(report, f"Linked Claude config CLAUDE.md at {target_path}")
    return True


def ensure_openbase_instruction_md(
    workspace_dir: str | Path,
    *,
    target_path: Path,
    document_label: str,
    include_normal_codex_agents: bool | None = None,
    report: Callable[[str], None] | None = None,
) -> bool:
    """Maintain an editable agent instruction file with an Openbase section."""
    from openbase_coder_cli.services.console_settings import (
        include_normal_codex_agents_in_openbase_agents,
    )

    source_path = _agents_source_path(workspace_dir)
    if not source_path.is_file():
        _report(report, f"{document_label} source not found at {source_path}")
        return False

    source_text = source_path.read_text(encoding="utf-8")
    should_include_normal = (
        include_normal_codex_agents_in_openbase_agents()
        if include_normal_codex_agents is None
        else include_normal_codex_agents
    )
    generated_section = _generated_agents_md(
        source_text,
        source_path,
        include_normal_codex_agents=should_include_normal,
    )
    if target_path.is_symlink():
        target_path.unlink()
    elif target_path.exists():
        if not target_path.is_file():
            _report(
                report,
                f"{document_label} already exists at {target_path}; leaving it unchanged.",
            )
            return False

    updated = generated_section

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() and not target_path.is_file():
        _report(
            report,
            f"{document_label} already exists at {target_path}; leaving it unchanged.",
        )
        return False

    if target_path.exists() and target_path.read_text(encoding="utf-8") == updated:
        _report(report, f"{document_label} already configured at {target_path}")
        return False

    target_path.write_text(updated, encoding="utf-8")
    _report(report, f"Updated editable {document_label} at {target_path}")
    return True


def _generated_agents_md(
    source_text: str,
    source_path: Path,
    *,
    include_normal_codex_agents: bool,
) -> str:
    sections: list[str] = []
    normal_agents = (
        _normal_codex_agents_section(source_path) if include_normal_codex_agents else ""
    )
    if normal_agents:
        sections.append(normal_agents)
    sections.append(_managed_agents_md_section(source_text, source_path))
    return (
        "\n\n".join(section.rstrip() for section in sections if section.strip()) + "\n"
    )


def _normal_codex_agents_section(openbase_source_path: Path) -> str:
    normal_path = NORMAL_CODEX_AGENTS_MD_PATH.expanduser()
    try:
        if normal_path.resolve() == openbase_source_path.resolve():
            return ""
    except OSError:
        return ""
    if not normal_path.is_file():
        return ""
    content = normal_path.read_text(encoding="utf-8").strip()
    if not content:
        return ""
    return (
        "## Non-Openbase Instructions\n\n"
        f"- These instructions are included from {normal_path}.\n\n"
        f"{content}\n"
    )


def _managed_agents_md_section(source_text: str, source_path: Path) -> str:
    body = _without_h2_headings(render_instruction_template(source_text)).strip()
    note = f"- These instructions are auto generated from {source_path}."
    if body:
        return f"{MANAGED_AGENTS_HEADING}\n\n{note}\n\n{body}\n"
    return f"{MANAGED_AGENTS_HEADING}\n\n{note}\n"


def ensure_rendered_instruction_file(
    source_path: Path,
    target_path: Path,
    *,
    document_label: str,
    force: bool = False,
    report: Callable[[str], None] | None = None,
) -> bool:
    if not source_path.is_file():
        _report(report, f"{document_label} source not found at {source_path}")
        return False

    standalone = is_standalone_runtime()
    if standalone:
        # Standalone installs have no editable instruction source: the
        # packaged templates are the only authority, so rendered files are
        # always regenerated (local edits never stick) and kept read-only so
        # they don't invite editing.
        force = True

    source_text = source_path.read_text(encoding="utf-8")
    rendered = _rendered_instruction_file(source_text, source_path)
    existing = ""
    if target_path.exists() or target_path.is_symlink():
        try:
            existing = target_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    if target_path.is_symlink():
        target_path.unlink()
    elif target_path.exists():
        if not target_path.is_file():
            _report(
                report,
                f"{document_label} already exists at {target_path}; leaving it unchanged.",
            )
            return False
        # A file carrying the generated-from header is machine-managed (the
        # header itself directs edits to the source template), so template
        # updates must propagate; only unmarked files can be user-authored.
        if (
            not force
            and not existing.startswith(GENERATED_INSTRUCTION_PREFIX)
            and (
                existing != rendered
                and existing
                != _rendered_instruction_file(source_text, source_path, render=False)
                and not text_matches_instruction_template(
                    _without_generated_instruction_header(existing),
                    source_text,
                )
            )
        ):
            _report(
                report,
                f"{document_label} already exists at {target_path} and differs from the workspace default; leaving it unchanged.",
            )
            return False

    if existing == rendered and target_path.exists():
        if standalone:
            _mark_read_only(target_path)
        _report(report, f"{document_label} already configured at {target_path}")
        return False

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.is_file() and not os.access(target_path, os.W_OK):
        target_path.chmod(0o644)
    target_path.write_text(rendered, encoding="utf-8")
    if standalone:
        _mark_read_only(target_path)
    _report(report, f"Updated {document_label} at {target_path}")
    return True


def _mark_read_only(target_path: Path) -> None:
    try:
        target_path.chmod(0o444)
    except OSError:
        pass


def _rendered_instruction_file(
    source_text: str,
    source_path: Path,
    *,
    render: bool = True,
) -> str:
    body = render_instruction_template(source_text) if render else source_text
    return (
        f"{GENERATED_INSTRUCTION_PREFIX}{source_path}; "
        "edit the source template instead. -->\n\n"
        f"{body}"
    )


def _without_generated_instruction_header(value: str) -> str:
    if not value.startswith(GENERATED_INSTRUCTION_PREFIX):
        return value
    marker = "-->"
    header_end = value.find(marker)
    if header_end == -1:
        return value
    remainder = value[header_end + len(marker) :]
    return remainder[2:] if remainder.startswith("\n\n") else remainder.lstrip("\n")


def _agents_source_path(workspace_dir: str | Path) -> Path:
    workspace_path = Path(workspace_dir)
    if workspace_path.name == CODEX_HOME_DEFAULT_SOURCE_DIR:
        return workspace_path / "AGENTS.md"
    return workspace_path / CODEX_HOME_DEFAULT_SOURCE_DIR / "AGENTS.md"


def _instruction_source_root(workspace_dir: str | Path | None) -> Path | None:
    if workspace_dir:
        workspace_source = Path(workspace_dir) / CODEX_HOME_DEFAULT_SOURCE_DIR
        if workspace_source.is_dir():
            return workspace_source
    return packaged_instructions_dir()


def _without_h2_headings(text: str) -> str:
    return "".join(
        f"#{line}" if line.startswith("## ") else line
        for line in text.splitlines(keepends=True)
    )


def _replace_managed_agents_md_section(existing: str, generated_section: str) -> str:
    lines = existing.splitlines(keepends=True)
    start_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip() == MANAGED_AGENTS_HEADING
        ),
        None,
    )

    if start_index is None:
        prefix = existing.rstrip()
        if not prefix:
            return generated_section
        return f"{prefix}\n\n{generated_section}"

    end_index = len(lines)
    for index in range(start_index + 1, len(lines)):
        line = lines[index]
        if line.startswith("## ") and line.strip() != MANAGED_AGENTS_HEADING:
            end_index = index
            break

    prefix = "".join(lines[:start_index]).rstrip()
    suffix = "".join(lines[end_index:]).lstrip()

    if prefix and suffix:
        return f"{prefix}\n\n{generated_section}\n{suffix}"
    if prefix:
        return f"{prefix}\n\n{generated_section}"
    if suffix:
        return f"{generated_section}\n{suffix}"
    return generated_section


def _report(report: Callable[[str], None] | None, message: str) -> None:
    if report is not None:
        report(message)
