"""Codex config phase: shared-home MCP registration, instructions, and skills.

Openbase runs against the user's real ``~/.codex``. Setup only registers the
super-agents MCP server (plus the session-ID hook) there; Openbase's
full-permission posture is passed per session by super-agents, never written
into the shared config.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from shutil import which

import click

from openbase_coder_cli.backend_config import (
    CODEX_BACKEND,
    DEFAULT_CODING_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
    SUPER_AGENTS_DEFAULT_BACKEND_ENV_KEY,
)
from openbase_coder_cli.cli.setup.hooks import ensure_codex_session_id_hook
from openbase_coder_cli.codex_home_instructions import (
    ensure_openbase_agents_md,
    ensure_rendered_instruction_file,
)
from openbase_coder_cli.paths import (
    CLAUDE_CONFIG_DIR,
    CODEX_CONFIG_PATH,
    CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH,
    CODEX_DISPATCHER_INSTRUCTIONS_PATH,
    CODEX_HOME_DIR,
    CODEX_SUPER_AGENT_INSTRUCTIONS_PATH,
    OPENBASE_BASE_DIR,
)
from openbase_coder_cli.runtime import (
    current_runtime_package,
    packaged_instructions_dir,
    packaged_skills_dir,
    stable_package_path,
)

logger = logging.getLogger(__name__)

CODEX_HOME_DEFAULT_SOURCE_DIR = "instructions"
CODEX_HOME_SKILLS_SOURCE_DIR = "skills"
OPENBASE_INSTRUCTION_FILES = (
    ("VOICE_INSTRUCTIONS.md", CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH),
    ("DISPATCHER_INSTRUCTIONS.md", CODEX_DISPATCHER_INSTRUCTIONS_PATH),
    ("SUPER_AGENT_INSTRUCTIONS.md", CODEX_SUPER_AGENT_INSTRUCTIONS_PATH),
)
SUPER_AGENTS_MCP_TABLE = "mcp_servers.super-agents"
SUPER_AGENTS_MCP_COMMAND = "super-agents-mcp"
# Openbase sessions run without native Codex gating: approvals are handled by
# the Openbase approvals layer, so super-agents passes these per thread.
SUPER_AGENTS_CODEX_APPROVAL_POLICY_ENV = "SUPER_AGENTS_CODEX_APPROVAL_POLICY"
SUPER_AGENTS_CODEX_SANDBOX_POLICY_ENV = "SUPER_AGENTS_CODEX_SANDBOX_POLICY"
SUPER_AGENTS_CODEX_APPROVAL_POLICY = "never"
SUPER_AGENTS_CODEX_SANDBOX_POLICY = "danger-full-access"


def _ensure_openbase_instruction_files(workspace_dir: str) -> None:
    """Render Openbase-managed agent instruction files under ~/.openbase."""
    defaults_dir = _default_instructions_dir(workspace_dir)

    ensure_openbase_agents_md(
        defaults_dir.parent,
        report=click.echo,
    )

    for resource_name, target_path in OPENBASE_INSTRUCTION_FILES:
        source_path = defaults_dir / resource_name
        ensure_rendered_instruction_file(
            source_path,
            target_path=target_path,
            document_label=f"Openbase instruction {resource_name}",
            report=click.echo,
        )


def _symlink_codex_home_skills(
    workspace_dir: str,
    *,
    report: Callable[[str], None] = click.echo,
) -> None:
    """Symlink workspace-owned skills into both shared agent homes."""
    source_root = _default_skills_dir(workspace_dir)
    skill_sources = _workspace_skill_sources(source_root)
    if not skill_sources:
        report(f"No workspace skills found at {source_root}")
        return

    _symlink_skills_to_root(
        skill_sources,
        target_root=CODEX_HOME_DIR / "skills",
        label="Codex home",
        report=report,
    )
    _symlink_skills_to_root(
        skill_sources,
        target_root=CLAUDE_CONFIG_DIR / "skills",
        label="Claude config",
        report=report,
    )


def relink_workspace_skills_from_installation(
    *,
    report: Callable[[str], None] | None = None,
) -> bool:
    """Re-point bundled workspace skill links at this machine's checkout.

    Skill links in the agent homes are machine-local symlinks, but the
    directories that hold them can be replicated between machines by file
    sync — after which they carry the OTHER machine's home paths and dangle
    here (skills silently vanish from agents and the dashboard). The linker
    already replaces wrong-target links, so running it at service startup
    lets each machine self-heal.
    """
    from openbase_coder_cli.services.installation import InstallationConfig

    emit = report or logger.info
    try:
        if not InstallationConfig.exists():
            return False
        config = InstallationConfig.load()
        if not config.workspace_path:
            return False
        _symlink_codex_home_skills(config.workspace_path, report=emit)
        return True
    except Exception:
        logger.warning("Unable to relink workspace skills", exc_info=True)
        return False


def _symlink_skills_to_root(
    skill_sources: list[Path],
    *,
    target_root: Path,
    label: str,
    report: Callable[[str], None] = click.echo,
) -> None:
    target_root.mkdir(parents=True, exist_ok=True)

    for source_path in skill_sources:
        target_path = target_root / source_path.name
        if target_path.is_symlink():
            # Compare the literal link target, not the resolved directory: a
            # link pinned to a versioned release dir resolves identically to
            # the stable current/ alias today but dangles after rotation.
            if target_path.readlink() == source_path:
                report(f"{label} skill already linked at {target_path}")
                continue
            target_path.unlink()
        elif target_path.exists():
            report(
                f"{label} skill already exists at {target_path}; leaving it unchanged."
            )
            continue

        target_path.symlink_to(source_path)
        report(f"Linked {label} skill {target_path} -> {source_path}")


def _ensure_codex_config(
    workspace_dir: str,
    *,
    coding_backend: str = DEFAULT_CODING_BACKEND,
) -> None:
    """Register super-agents (and the session-ID hook) in the shared ~/.codex.

    Only the MCP table and the trusted hook — the user's own model, sandbox,
    and approval settings are never touched. Openbase sessions get their
    permission posture per thread from super-agents.
    """
    config_path = CODEX_CONFIG_PATH
    command_path, args = _super_agents_mcp_command(Path(workspace_dir))
    block = (
        f"[{SUPER_AGENTS_MCP_TABLE}]\n"
        f"command = {json.dumps(str(command_path))}\n"
        f"{_toml_args_line(args)}"
        f"{_toml_env_line(_codex_child_backend(coding_backend))}"
    )

    if not command_path.is_file():
        click.echo(
            f"Super Agents MCP command not found at {command_path}; "
            "writing the expected config path anyway."
        )

    existing = ""
    if config_path.is_file():
        existing = config_path.read_text(encoding="utf-8")

    updated = _replace_toml_table(existing, SUPER_AGENTS_MCP_TABLE, block)
    if updated == existing:
        click.echo(f"Codex config already has super-agents at {config_path}")
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(updated, encoding="utf-8")
        click.echo(f"Registered super-agents MCP in Codex config {config_path}")

    ensure_codex_session_id_hook(config_path)


def _super_agents_mcp_command(workspace_dir: Path) -> tuple[Path, list[str]]:
    # An empty workspace_dir means standalone mode; never emit paths relative
    # to whatever directory setup happened to run from.
    has_workspace = bool(str(workspace_dir).strip()) and str(workspace_dir) != "."
    candidates = (
        workspace_dir / ".venv" / "bin" / SUPER_AGENTS_MCP_COMMAND,
        workspace_dir / "cli" / ".venv" / "bin" / SUPER_AGENTS_MCP_COMMAND,
    )
    if has_workspace:
        for candidate in candidates:
            if candidate.is_file():
                return candidate, []

    runtime_package = current_runtime_package()
    if runtime_package is not None:
        bundled_command = runtime_package.python_path.parent / SUPER_AGENTS_MCP_COMMAND
        if bundled_command.is_file():
            # Persisted into MCP configs: must survive release rotation.
            return stable_package_path(bundled_command), []

    if command := which(SUPER_AGENTS_MCP_COMMAND):
        return stable_package_path(Path(command)), []

    if has_workspace and (uv_bin := which("uv")):
        run_dir = workspace_dir / "cli"
        if not run_dir.is_dir():
            run_dir = workspace_dir
        return Path(uv_bin), [
            "--directory",
            str(run_dir),
            "run",
            SUPER_AGENTS_MCP_COMMAND,
        ]

    return candidates[0], []


def _default_instructions_dir(workspace_dir: str) -> Path:
    if workspace_dir:
        # May not exist; callers skip missing instruction files.
        return Path(workspace_dir) / CODEX_HOME_DEFAULT_SOURCE_DIR
    packaged = packaged_instructions_dir()
    if packaged is not None:
        return packaged
    raise click.ClickException(
        "No instructions source found: the bundled runtime package does not "
        "provide an instructions directory."
    )


def _default_skills_dir(workspace_dir: str) -> Path:
    if workspace_dir:
        workspace_source = Path(workspace_dir) / CODEX_HOME_SKILLS_SOURCE_DIR
        if workspace_source.is_dir():
            return workspace_source
    packaged = packaged_skills_dir()
    if packaged is not None:
        # Symlink targets are created from this root: must survive rotation.
        return stable_package_path(packaged)
    # Missing skills are non-fatal; the caller reports and continues.
    return Path(workspace_dir or str(OPENBASE_BASE_DIR)) / CODEX_HOME_SKILLS_SOURCE_DIR


def _toml_args_line(args: list[str]) -> str:
    if not args:
        return ""
    return f"args = {json.dumps(args)}\n"


def _toml_env_line(backend: str) -> str:
    env_pairs = (
        (SUPER_AGENTS_DEFAULT_BACKEND_ENV_KEY, backend),
        (SUPER_AGENTS_CODEX_APPROVAL_POLICY_ENV, SUPER_AGENTS_CODEX_APPROVAL_POLICY),
        (SUPER_AGENTS_CODEX_SANDBOX_POLICY_ENV, SUPER_AGENTS_CODEX_SANDBOX_POLICY),
    )
    body = ", ".join(f"{key} = {json.dumps(value)}" for key, value in env_pairs)
    return f"env = {{ {body} }}\n"


def _codex_child_backend(coding_backend: str) -> str:
    if coding_backend == OPENBASE_CLOUD_CODEX_BACKEND:
        return OPENBASE_CLOUD_CODEX_BACKEND
    return CODEX_BACKEND


def _replace_toml_table(text: str, table_name: str, block: str) -> str:
    target_header = f"[{table_name}]"
    lines = text.splitlines()
    output: list[str] = []
    index = 0

    while index < len(lines):
        if lines[index].strip() == target_header:
            index += 1
            while index < len(lines):
                stripped = lines[index].strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    break
                index += 1
            while output and not output[-1].strip():
                output.pop()
            continue

        output.append(lines[index])
        index += 1

    while output and not output[-1].strip():
        output.pop()

    if output:
        return "\n".join(output) + "\n\n" + block
    return block


def _workspace_skill_sources(source_root: Path) -> list[Path]:
    candidate_roots = [source_root / "skills", source_root]
    seen: set[Path] = set()
    sources: list[Path] = []

    for candidate_root in candidate_roots:
        if not candidate_root.is_dir():
            continue
        for child in sorted(candidate_root.iterdir(), key=lambda path: path.name):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if (child / "SKILL.md").is_file():
                resolved = child.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    sources.append(child)

    return sources
