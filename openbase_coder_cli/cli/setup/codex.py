"""Codex config phase: service Codex home auth, config, instructions, and skills."""

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
from openbase_coder_cli.codex_backend_config import apply_backend_to_codex_config
from openbase_coder_cli.codex_home_instructions import (
    ensure_openbase_agents_md,
    ensure_openbase_claude_md_symlink,
    ensure_rendered_instruction_file,
)
from openbase_coder_cli.paths import (
    CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH,
    CODEX_DISPATCHER_INSTRUCTIONS_PATH,
    CODEX_HOME_DIR,
    CODEX_SUPER_AGENT_INSTRUCTIONS_PATH,
    NORMAL_CODEX_CONFIG_PATH,
    OPENBASE_BASE_DIR,
    OPENBASE_CLAUDE_CONFIG_DIR,
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
CODEX_HOME_DEFAULT_FILES = (
    ("VOICE_INSTRUCTIONS.md", CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH),
    ("DISPATCHER_INSTRUCTIONS.md", CODEX_DISPATCHER_INSTRUCTIONS_PATH),
    ("SUPER_AGENT_INSTRUCTIONS.md", CODEX_SUPER_AGENT_INSTRUCTIONS_PATH),
)
SUPER_AGENTS_MCP_TABLE = "mcp_servers.super-agents"
SUPER_AGENTS_MCP_COMMAND = "super-agents-mcp"
CODEX_HOME_PERMISSION_VALUES = (
    ("sandbox_mode", json.dumps("danger-full-access")),
    (
        "approval_policy",
        "{ granular = { sandbox_approval = false, rules = false, "
        "mcp_elicitations = false, request_permissions = false, "
        "skill_approval = false } }",
    ),
)


def _symlink_codex_auth() -> None:
    """Point the service CODEX_HOME at the user's normal Codex login."""
    codex_auth = Path.home() / ".codex" / "auth.json"
    service_auth = CODEX_HOME_DIR / "auth.json"

    CODEX_HOME_DIR.mkdir(parents=True, exist_ok=True)

    if not codex_auth.is_file():
        # Still link (dangling until `codex login` writes the file) so a
        # post-setup login is picked up without re-running setup.
        click.echo(
            f"Codex auth not found at {codex_auth}; run 'codex login' before "
            "using voice Codex services."
        )

    if service_auth.is_symlink():
        if service_auth.resolve() == codex_auth.resolve():
            click.echo(f"Codex service auth already linked to {codex_auth}")
            return
        service_auth.unlink()
    elif service_auth.exists():
        try:
            auth_matches = service_auth.read_bytes() == codex_auth.read_bytes()
        except OSError:
            auth_matches = False
        if not auth_matches:
            click.echo(
                f"Codex service auth already exists at {service_auth} and differs "
                f"from {codex_auth}; leaving it unchanged."
            )
            return
        service_auth.unlink()

    service_auth.symlink_to(codex_auth)
    click.echo(f"Symlinked Codex service auth → {codex_auth}")


def _ensure_codex_home_default_files(workspace_dir: str) -> None:
    """Create Openbase-managed agent instruction files."""
    CODEX_HOME_DIR.mkdir(parents=True, exist_ok=True)
    OPENBASE_CLAUDE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    defaults_dir = _default_instructions_dir(workspace_dir)

    ensure_openbase_agents_md(
        defaults_dir.parent,
        codex_home_dir=CODEX_HOME_DIR,
        report=click.echo,
    )
    ensure_openbase_claude_md_symlink(report=click.echo)

    for resource_name, target_path in CODEX_HOME_DEFAULT_FILES:
        source_path = defaults_dir / resource_name
        ensure_rendered_instruction_file(
            source_path,
            target_path=target_path,
            document_label=f"Openbase instruction {resource_name}",
            report=click.echo,
        )


def _ensure_matching_symlink_or_file(
    *,
    target_path: Path,
    source_path: Path,
    label: str,
) -> bool:
    if target_path.is_symlink():
        if target_path.resolve() == source_path.resolve():
            click.echo(f"{label} already linked at {target_path}")
            return False
        target_path.unlink()
    elif target_path.exists():
        if not target_path.is_file():
            click.echo(
                f"{label} already exists at {target_path}; leaving it unchanged."
            )
            return False

        try:
            default_matches = target_path.read_bytes() == source_path.read_bytes()
        except OSError:
            default_matches = False
        if not default_matches:
            click.echo(
                f"{label} already exists at {target_path} and differs from "
                "the workspace default; leaving it unchanged."
            )
            return False
        target_path.unlink()

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.symlink_to(source_path)
    click.echo(f"Linked {label} {target_path} -> {source_path}")
    return True


def _symlink_codex_home_skills(
    workspace_dir: str,
    *,
    report: Callable[[str], None] = click.echo,
) -> None:
    """Symlink workspace-owned skills into Openbase-managed agent homes."""
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
        target_root=OPENBASE_CLAUDE_CONFIG_DIR / "skills",
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


def _ensure_codex_home_config(
    workspace_dir: str,
    *,
    coding_backend: str = DEFAULT_CODING_BACKEND,
    link_codex_config: bool = False,
) -> None:
    """Configure Openbase's service Codex home."""
    CODEX_HOME_DIR.mkdir(parents=True, exist_ok=True)
    config_path = CODEX_HOME_DIR / "config.toml"
    if link_codex_config:
        _symlink_codex_home_config()

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

    updated = _ensure_toml_root_values(existing, CODEX_HOME_PERMISSION_VALUES)
    updated = _replace_toml_table(updated, SUPER_AGENTS_MCP_TABLE, block)
    if updated == existing:
        click.echo(f"Codex home config already configured at {config_path}")
    else:
        config_path.write_text(updated, encoding="utf-8")
        click.echo(f"Configured Codex home config at {config_path}")

    ensure_codex_session_id_hook(config_path)

    result = apply_backend_to_codex_config(coding_backend, config_path=config_path)
    if result.changed:
        click.echo(f"Configured Codex backend in {result.path}")


def _ensure_normal_codex_mcp(workspace_dir: str) -> None:
    """Register the super-agents MCP server in the user's normal Codex home.

    Only the MCP table — never the Openbase permission overrides. Users can
    remove the entry; an explicit setup re-run restores it.
    """
    config_path = NORMAL_CODEX_CONFIG_PATH
    command_path, args = _super_agents_mcp_command(Path(workspace_dir))
    block = (
        f"[{SUPER_AGENTS_MCP_TABLE}]\n"
        f"command = {json.dumps(str(command_path))}\n"
        f"{_toml_args_line(args)}"
        f"{_toml_env_line(CODEX_BACKEND)}"
    )

    existing = ""
    if config_path.is_file():
        existing = config_path.read_text(encoding="utf-8")

    updated = _replace_toml_table(existing, SUPER_AGENTS_MCP_TABLE, block)
    if updated == existing:
        click.echo(f"Normal Codex config already has super-agents at {config_path}")
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(updated, encoding="utf-8")
    click.echo(f"Registered super-agents MCP in normal Codex config {config_path}")


def _symlink_codex_home_config() -> None:
    """Point the service CODEX_HOME config at the user's normal Codex config."""
    service_config = CODEX_HOME_DIR / "config.toml"
    normal_config = NORMAL_CODEX_CONFIG_PATH

    CODEX_HOME_DIR.mkdir(parents=True, exist_ok=True)
    normal_config.parent.mkdir(parents=True, exist_ok=True)

    if normal_config.exists() and not normal_config.is_file():
        raise click.ClickException(
            f"Normal Codex config exists but is not a file: {normal_config}"
        )

    if service_config.is_symlink():
        if service_config.resolve() == normal_config.resolve():
            click.echo(f"Codex home config already linked to {normal_config}")
            return
        service_config.unlink()
    elif service_config.exists():
        if not service_config.is_file():
            raise click.ClickException(
                f"Codex home config exists but is not a file: {service_config}"
            )
        if not normal_config.exists():
            normal_config.write_text(
                service_config.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        service_config.unlink()

    if not normal_config.exists():
        normal_config.write_text("", encoding="utf-8")

    service_config.symlink_to(normal_config)
    click.echo(f"Symlinked Codex home config -> {normal_config}")


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
    return (
        f"env = {{ {SUPER_AGENTS_DEFAULT_BACKEND_ENV_KEY} = {json.dumps(backend)} }}\n"
    )


def _codex_child_backend(coding_backend: str) -> str:
    if coding_backend == OPENBASE_CLOUD_CODEX_BACKEND:
        return OPENBASE_CLOUD_CODEX_BACKEND
    return CODEX_BACKEND


def _ensure_toml_root_values(
    text: str,
    values: tuple[tuple[str, str], ...],
) -> str:
    lines = text.splitlines()
    first_table_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().startswith("[") and line.strip().endswith("]")
        ),
        len(lines),
    )
    root_lines = lines[:first_table_index]
    table_lines = lines[first_table_index:]
    keys = {key for key, _value in values}
    updated_root = [line for line in root_lines if _toml_root_key(line) not in keys]

    while updated_root and not updated_root[-1].strip():
        updated_root.pop()

    for key, value in values:
        updated_root.append(f"{key} = {value}")

    while table_lines and not table_lines[0].strip():
        table_lines.pop(0)

    if table_lines:
        return "\n".join(updated_root) + "\n\n" + "\n".join(table_lines) + "\n"
    return "\n".join(updated_root) + "\n"


def _toml_root_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


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
