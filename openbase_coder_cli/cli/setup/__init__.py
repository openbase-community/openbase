"""Setup command orchestrator for the Openbase Coder install flow.

Phase implementations live in sibling modules (workspace, env, codex,
dispatcher, claude). Names are re-exported here so existing imports of
``openbase_coder_cli.cli.setup`` keep working.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from pathlib import Path
from shutil import which  # noqa: F401

import click

from openbase_coder_cli.backend_binaries import ensure_backend_binary
from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    CODING_BACKEND_ENV_KEY,  # noqa: F401
    DEFAULT_CODING_BACKEND,  # noqa: F401
    OPENBASE_CLOUD_BACKEND,
    SUPPORTED_BACKENDS,
    normalize_backend,
)
from openbase_coder_cli.claude_auth import (
    claude_auth_status,  # noqa: F401
    run_claude_login,  # noqa: F401
    sync_normal_claude_state,  # noqa: F401
)
from openbase_coder_cli.cli.node import run_workspace_package_command  # noqa: F401
from openbase_coder_cli.cli.setup.claude import (
    CLAUDE_CODE_PERMISSION_MODE,  # noqa: F401
    OPENBASE_CLAUDE_SETTINGS_DEFAULTS,  # noqa: F401
    _ensure_claude_auth_bridge,
    _ensure_claude_config,
    _ensure_claude_settings,  # noqa: F401
    _ensure_normal_claude_mcp,
    _ensure_normal_claude_md_symlink,
    _merge_claude_md_excludes,  # noqa: F401
    _merge_claude_settings,  # noqa: F401
    _read_json_object,  # noqa: F401
)
from openbase_coder_cli.cli.setup.codex import (
    CODEX_HOME_DEFAULT_FILES,  # noqa: F401
    CODEX_HOME_DEFAULT_SOURCE_DIR,  # noqa: F401
    CODEX_HOME_PERMISSION_VALUES,  # noqa: F401
    CODEX_HOME_SKILLS_SOURCE_DIR,  # noqa: F401
    SUPER_AGENTS_MCP_COMMAND,  # noqa: F401
    SUPER_AGENTS_MCP_TABLE,  # noqa: F401
    _default_instructions_dir,  # noqa: F401
    _default_skills_dir,  # noqa: F401
    _ensure_codex_home_config,
    _ensure_codex_home_default_files,
    _ensure_matching_symlink_or_file,  # noqa: F401
    _ensure_normal_codex_mcp,
    _ensure_toml_root_values,  # noqa: F401
    _replace_toml_table,  # noqa: F401
    _super_agents_mcp_command,  # noqa: F401
    _symlink_codex_auth,
    _symlink_codex_home_config,  # noqa: F401
    _symlink_codex_home_skills,
    _symlink_skills_to_root,  # noqa: F401
    _toml_args_line,  # noqa: F401
    _toml_root_key,  # noqa: F401
    _workspace_skill_sources,  # noqa: F401
)
from openbase_coder_cli.cli.setup.dispatcher import (
    AUDIO_PROVIDER_CARTESIA,  # noqa: F401
    AUDIO_PROVIDER_LOCAL,
    AUDIO_PROVIDER_OPENBASE_CLOUD,  # noqa: F401
    AUDIO_PROVIDER_OPTIONS,
    CODEX_HOME_DEFAULT_DISPATCHER_CONFIG,  # noqa: F401
    DEFAULT_AUDIO_PROVIDER,  # noqa: F401
    LOCAL_AUDIO_REQUIREMENTS,  # noqa: F401
    LOCAL_AUDIO_SPACY_MODEL,  # noqa: F401
    _audio_provider_config,  # noqa: F401
    _default_dispatcher_config,  # noqa: F401
    _download_local_audio_models,
    _ensure_codex_home_dispatcher_config,
    _ensure_local_audio_dependencies,
    _local_audio_dependencies_available,  # noqa: F401
    _update_dispatcher_audio_provider,  # noqa: F401
)
from openbase_coder_cli.cli.setup.env import (
    _ensure_env_file,
    _ensure_openbase_cloud_machine_token,
    _env_file_values,  # noqa: F401
    _missing_livekit_client_credential_values,  # noqa: F401
    _selected_coding_backend,
    _upsert_env_file_values,  # noqa: F401
)
from openbase_coder_cli.cli.setup.workspace import (
    BUNDLED_SOUND_FILES,  # noqa: F401
    BUNDLED_SOUNDS_PACKAGE,  # noqa: F401
    DEFAULT_SYNCTHING_GLOBAL_STIGNORE_CONTENT,  # noqa: F401
    THREAD_SYNC_EXCHANGE_DIR_NAME,  # noqa: F401
    THREAD_SYNC_MARKER_FILE_NAME,  # noqa: F401
    THREAD_SYNC_STIGNORE_CONTENT,  # noqa: F401
    _build_console,
    _copy_bundled_sound,  # noqa: F401
    _ensure_bundled_sounds,
    _ensure_thread_sync_exchange_dir,
    _init_cli_workspace,
    _init_standalone_runtime,
    _install_cli_shim,
    _syncthing_global_ignore_path,  # noqa: F401
    resolve_dev_workspace_dir,
)
from openbase_coder_cli.codex_backend_config import (
    apply_backend_to_codex_config,  # noqa: F401
)
from openbase_coder_cli.codex_home_instructions import (
    ensure_openbase_agents_md,  # noqa: F401
    ensure_openbase_claude_md_symlink,  # noqa: F401
    ensure_rendered_instruction_file,  # noqa: F401
)
from openbase_coder_cli.config.machine_token_manager import (
    MachineTokenError,  # noqa: F401
    MachineTokenManager,  # noqa: F401
)
from openbase_coder_cli.config.token_manager import (
    DEFAULT_WEB_BACKEND_URL,  # noqa: F401
    AuthLoginRequiredError,  # noqa: F401
    AuthTransientError,  # noqa: F401
    TokenManager,  # noqa: F401
)
from openbase_coder_cli.dispatcher_config import (
    DISPATCHER_VOICE_ID_KEY,  # noqa: F401
    DISPATCHER_VOICE_NAME_KEY,  # noqa: F401
    STT_PROVIDER_KEY,  # noqa: F401
    TTS_PROVIDER_KEY,  # noqa: F401
    set_dispatcher_service_tier,
)
from openbase_coder_cli.paths import (
    CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH,  # noqa: F401
    CODEX_DISPATCHER_CONFIG_PATH,  # noqa: F401
    CODEX_DISPATCHER_INSTRUCTIONS_PATH,  # noqa: F401
    CODEX_HOME_DIR,  # noqa: F401
    CODEX_SUPER_AGENT_INSTRUCTIONS_PATH,  # noqa: F401
    DEFAULT_ENV_FILE_PATH,
    NORMAL_CLAUDE_CONFIG_DIR,  # noqa: F401
    NORMAL_CLAUDE_SETTINGS_PATH,  # noqa: F401
    NORMAL_CODEX_AGENTS_MD_PATH,  # noqa: F401
    NORMAL_CODEX_CONFIG_PATH,  # noqa: F401
    OPENBASE_BASE_DIR,
    OPENBASE_CLAUDE_CONFIG_DIR,  # noqa: F401
    OPENBASE_CLAUDE_JSON_PATH,  # noqa: F401
    OPENBASE_CLAUDE_SETTINGS_PATH,  # noqa: F401
    OPENBASE_SOUNDS_DIR,  # noqa: F401
)
from openbase_coder_cli.runtime import (
    current_runtime_package,
    packaged_instructions_dir,  # noqa: F401
    packaged_skills_dir,  # noqa: F401
)
from openbase_coder_cli.services.installation import InstallationConfig
from openbase_coder_cli.services.launchd import install_all_services
from openbase_coder_cli.services.onboarding import compute_cli_configured
from openbase_coder_cli.services.tailscale_serve import (
    configure_tailscale_serve,
    tailscale_serve_health,
)
from openbase_coder_cli.stt_providers import (
    ASSEMBLYAI_STT_PROVIDER_ID,  # noqa: F401
    LOCAL_MLX_WHISPER_STT_PROVIDER_ID,  # noqa: F401
    OPENBASE_CLOUD_STT_PROVIDER_ID,  # noqa: F401
    download_local_mlx_whisper,  # noqa: F401
)
from openbase_coder_cli.tts_providers import (
    CARTESIA_PROVIDER_ID,  # noqa: F401
    KOKORO_PROVIDER_ID,  # noqa: F401
    OPENBASE_CLOUD_TTS_PROVIDER_ID,  # noqa: F401
    get_tts_provider,  # noqa: F401
)

CODING_BACKEND_OPTIONS = SUPPORTED_BACKENDS
SETUP_PROGRESS_STEPS = (
    "workspace",
    "installation_config",
    "env",
    "agent_config",
    "services",
    "tailscale_serve",
)


class _SetupProgress:
    """Emit NDJSON step events for `setup --json-progress`.

    Event shapes and step ids are defined in the workspace
    ``specs/onboarding/README.md`` setup progress protocol. When enabled, the
    process's stdout fd is redirected to stderr so human-readable output
    (including subprocess output) stays off the NDJSON stream; events are
    written to the saved original stdout.
    """

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._current: str | None = None
        self._fd: int | None = None
        if enabled:
            self._fd = os.dup(1)
            os.dup2(2, 1)

    def step(self, step_id: str, step_status: str, detail: str | None = None) -> None:
        self._current = step_id if step_status == "start" else None
        self._emit(
            {
                "event": "step",
                "id": step_id,
                "status": step_status,
                "detail": detail,
            }
        )

    def abort(self, detail: str) -> None:
        if self._current:
            self._emit(
                {
                    "event": "step",
                    "id": self._current,
                    "status": "error",
                    "detail": detail,
                }
            )
        self._emit(
            {
                "event": "result",
                "ok": False,
                "cli_configured": False,
                "tailscale_serve_healthy": False,
            }
        )

    def result(self, *, cli_configured: bool, tailscale_serve_healthy: bool) -> None:
        self._emit(
            {
                "event": "result",
                "ok": True,
                "cli_configured": cli_configured,
                "tailscale_serve_healthy": tailscale_serve_healthy,
            }
        )

    def _emit(self, payload: dict[str, object]) -> None:
        if self._fd is None:
            return
        os.write(self._fd, (json.dumps(payload) + "\n").encode("utf-8"))


@click.command()
@click.option(
    "--workspace-dir",
    type=click.Path(),
    default=None,
    help=(
        "Path to your Openbase Coder workspace checkout (development mode). "
        "Discovered from the current installation or an editable CLI install "
        "when omitted."
    ),
)
@click.option(
    "--env-file",
    type=click.Path(),
    default=str(DEFAULT_ENV_FILE_PATH),
    show_default=True,
    help="Override .env file location.",
)
@click.option(
    "--assembly-ai-api-key",
    envvar="ASSEMBLY_AI_API_KEY",
    default="",
    help="AssemblyAI API key for speech-to-text.",
)
@click.option(
    "--cartesia-api-key",
    envvar="CARTESIA_API_KEY",
    default="",
    help="Cartesia API key for text-to-speech.",
)
@click.option(
    "--skip-services",
    is_flag=True,
    help="Skip background service installation.",
)
@click.option(
    "--link-codex-config",
    is_flag=True,
    help=(
        "Symlink Openbase's service Codex config to the normal ~/.codex/config.toml."
    ),
)
@click.option(
    "--link-claude-config",
    is_flag=True,
    help=(
        "Symlink Openbase's Claude settings to the normal ~/.claude/settings.json."
    ),
)
@click.option(
    "--fast-mode/--no-fast-mode",
    "fast_mode",
    default=True,
    show_default=True,
    help=(
        "Use the fast service tier for the voice dispatcher. Super Agents "
        "stay on the standard tier; both are adjustable in console settings."
    ),
)
@click.option(
    "--backend",
    "coding_backend",
    type=str,
    default=None,
    help=(
        "Default coding backend: codex, openbase-cloud, or claude-code. "
        "Prompted for when creating a new env file if omitted; "
        "existing env files are only changed when this option is provided."
    ),
)
@click.option(
    "--audio-provider",
    type=click.Choice(AUDIO_PROVIDER_OPTIONS),
    default=None,
    help=(
        "Voice audio provider. New dispatcher configs use openbase-cloud when "
        "omitted; existing configs are only changed when this option is provided."
    ),
)
@click.option(
    "--json-progress",
    is_flag=True,
    help=(
        "Emit NDJSON step events on stdout for UI-driven setup; "
        "human-readable output moves to stderr."
    ),
)
def setup(
    workspace_dir: str | None,
    env_file: str,
    assembly_ai_api_key: str,
    cartesia_api_key: str,
    skip_services: bool,
    link_codex_config: bool,
    link_claude_config: bool,
    fast_mode: bool,
    coding_backend: str | None,
    audio_provider: str | None,
    json_progress: bool,
) -> None:
    """Full install flow for Openbase Coder."""
    if platform.system() not in ("Darwin", "Linux"):
        raise click.ClickException("Setup is only supported on macOS and Linux.")
    if coding_backend is not None:
        try:
            coding_backend = normalize_backend(coding_backend)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    coding_backend = _require_backend_choice(
        env_file, coding_backend, interactive=not json_progress
    )

    progress = _SetupProgress(json_progress)
    try:
        serve_healthy = _run_setup_phases(
            progress,
            workspace_dir=workspace_dir,
            env_file=env_file,
            assembly_ai_api_key=assembly_ai_api_key,
            cartesia_api_key=cartesia_api_key,
            skip_services=skip_services,
            link_codex_config=link_codex_config,
            link_claude_config=link_claude_config,
            fast_mode=fast_mode,
            coding_backend=coding_backend,
            audio_provider=audio_provider,
        )
    except Exception as exc:
        progress.abort(str(exc))
        raise
    cli_configured = compute_cli_configured()
    progress.result(
        cli_configured=cli_configured, tailscale_serve_healthy=serve_healthy
    )

    click.echo()
    click.echo("Setup complete.")
    click.echo()
    click.echo(
        "To enable remote authentication, run 'openbase-coder login' "
        "and ensure OPENBASE_CODER_CLI_WEB_BACKEND_URL is set in your .env."
    )


def _require_backend_choice(
    env_file: str,
    coding_backend: str | None,
    *,
    interactive: bool,
) -> str | None:
    """Resolve the backend for a fresh install without preferring one.

    Existing env files keep their configured backend. New installs must pick
    one: interactively via a prompt, otherwise via --backend.
    """
    if coding_backend is not None or Path(env_file).is_file():
        return coding_backend
    if interactive and sys.stdin.isatty():
        choice = click.prompt(
            "Coding backend",
            type=click.Choice(("codex", "claude-code", "openbase-cloud")),
            show_choices=True,
        )
        return normalize_backend(choice)
    raise click.ClickException(
        "No coding backend configured yet. Pass --backend "
        "codex|claude-code|openbase-cloud for a first-time setup."
    )


def _run_setup_phases(
    progress: _SetupProgress,
    *,
    workspace_dir: str | None,
    env_file: str,
    assembly_ai_api_key: str,
    cartesia_api_key: str,
    skip_services: bool,
    link_codex_config: bool,
    link_claude_config: bool,
    fast_mode: bool,
    coding_backend: str | None,
    audio_provider: str | None,
) -> bool:
    """Run the setup phases, returning whether Tailscale Serve is healthy."""
    progress.step("workspace", "start")
    OPENBASE_BASE_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_thread_sync_exchange_dir()
    _ensure_bundled_sounds()
    runtime_package = current_runtime_package()
    use_dev_workspace = runtime_package is None

    # --- Locate runtime assets ---
    if runtime_package is not None:
        click.echo(f"Using bundled runtime assets from {runtime_package.root}")
        workspace_dir = ""
    else:
        workspace_dir = resolve_dev_workspace_dir(workspace_dir)
        click.echo(f"Using development workspace at {workspace_dir}")
    progress.step("workspace", "ok")

    # --- Write installation.json ---
    progress.step("installation_config", "start")
    config = InstallationConfig(
        workspace_path=workspace_dir if use_dev_workspace else "",
        env_file=env_file,
        package_path=str(runtime_package.root) if runtime_package else "",
        console_build_dir=(
            str(runtime_package.console_build_dir)
            if runtime_package and runtime_package.console_build_dir.is_dir()
            else ""
        ),
        python_path=(
            str(runtime_package.python_path)
            if runtime_package and runtime_package.python_path.is_file()
            else ""
        ),
        livekit_server_path=(
            str(runtime_package.livekit_server_path)
            if runtime_package and runtime_package.livekit_server_path.is_file()
            else ""
        ),
        standalone=runtime_package is not None,
    )
    config.save()
    click.echo("Wrote installation.json")
    progress.step("installation_config", "ok")

    # --- Generate .env ---
    progress.step("env", "start")
    _ensure_env_file(
        env_file,
        assembly_ai_api_key=assembly_ai_api_key,
        cartesia_api_key=cartesia_api_key,
        coding_backend=coding_backend,
    )
    selected_coding_backend = _selected_coding_backend(Path(env_file), coding_backend)
    if selected_coding_backend == OPENBASE_CLOUD_BACKEND:
        _ensure_openbase_cloud_machine_token(Path(env_file))
    progress.step("env", "ok")

    # --- Configure the selected coding backend (no codex/claude preference) ---
    progress.step("agent_config", "start")
    ensure_backend_binary(selected_coding_backend)
    if selected_coding_backend in (CODEX_BACKEND, OPENBASE_CLOUD_BACKEND):
        _symlink_codex_auth()
    _ensure_normal_claude_md_symlink()
    _ensure_codex_home_default_files(workspace_dir if use_dev_workspace else "")
    _ensure_codex_home_dispatcher_config(audio_provider=audio_provider)
    set_dispatcher_service_tier("fast" if fast_mode else "standard")
    click.echo(
        f"Voice dispatcher service tier: {'fast' if fast_mode else 'standard'} "
        "(Super Agents: standard; both adjustable in console settings)."
    )
    _symlink_codex_home_skills(workspace_dir if use_dev_workspace else "")

    # --- Initialize runtime assets ---
    if use_dev_workspace:
        _init_cli_workspace(
            workspace_dir,
            include_local_audio=audio_provider == AUDIO_PROVIDER_LOCAL,
        )
    else:
        _init_standalone_runtime(runtime_package)

    if audio_provider == AUDIO_PROVIDER_LOCAL:
        _ensure_local_audio_dependencies(runtime_package)
        _download_local_audio_models()

    # --- Configure the service CODEX_HOME ---
    if link_codex_config:
        _ensure_codex_home_config(
            workspace_dir if use_dev_workspace else "",
            coding_backend=selected_coding_backend,
            link_codex_config=True,
        )
    else:
        _ensure_codex_home_config(
            workspace_dir if use_dev_workspace else "",
            coding_backend=selected_coding_backend,
        )
    _ensure_claude_config(
        workspace_dir if use_dev_workspace else "",
        link_claude_config=link_claude_config,
    )
    _ensure_claude_auth_bridge(
        login_if_needed=selected_coding_backend == CLAUDE_CODE_BACKEND,
        required=selected_coding_backend == CLAUDE_CODE_BACKEND,
    )

    # --- Register super-agents MCP in the user's normal agent homes ---
    _ensure_normal_codex_mcp(workspace_dir if use_dev_workspace else "")
    _ensure_normal_claude_mcp(workspace_dir if use_dev_workspace else "")

    # --- Install/update user-facing CLI shim ---
    _install_cli_shim(workspace_dir if use_dev_workspace else "")

    # --- Build console ---
    if use_dev_workspace:
        _build_console(workspace_dir)
    elif config.console_build_dir:
        click.echo(f"Using bundled console build at {config.console_build_dir}")
    else:
        click.echo(
            "No bundled console build found; server will require a console build."
        )
    progress.step("agent_config", "ok")

    # --- Install services ---
    progress.step("services", "start")
    if not skip_services:
        click.echo()
        service_manager = "launchd" if platform.system() == "Darwin" else "systemd"
        click.echo(f"Installing {service_manager} services...")
        install_all_services(config)
        progress.step("services", "ok")
    else:
        click.echo("Skipped service installation (--skip-services).")
        progress.step("services", "ok", "skipped (--skip-services)")

    click.echo()
    click.echo("Configuring Tailscale Serve routes...")
    progress.step("tailscale_serve", "start")
    serve_healthy = False
    try:
        configure_tailscale_serve()
    except Exception as exc:
        click.echo(click.style(f"  WARN  {exc}", fg="yellow"))
        click.echo(
            "  Run these manually after Tailscale is installed and connected:\n"
            "    tailscale serve --bg --http=18080 http://127.0.0.1:7999\n"
            "    tailscale serve --bg --tcp=7880 tcp://127.0.0.1:7880"
        )
        progress.step("tailscale_serve", "warn", str(exc))
    else:
        health = tailscale_serve_health()
        # Services were installed seconds ago; give django time to boot
        # before declaring the external route unhealthy (fresh installs
        # otherwise warn with a transient 502).
        deadline = time.monotonic() + 30
        waited = False
        while (
            not health.healthy
            and health.openbase_configured
            and time.monotonic() < deadline
        ):
            if not waited:
                click.echo(
                    "  Waiting up to 30s for services to come up before "
                    "checking the external route..."
                )
                progress.step(
                    "tailscale_serve", "start", "waiting for services to boot"
                )
                waited = True
            time.sleep(3)
            health = tailscale_serve_health()
        serve_healthy = health.healthy
        if health.healthy:
            click.echo(f"  OK    Openbase is reachable at {health.openbase_url}")
            progress.step("tailscale_serve", "ok")
        else:
            click.echo(
                click.style(
                    "  WARN  Tailscale Serve was configured, but the external "
                    "Openbase health check is not passing.",
                    fg="yellow",
                )
            )
            if health.error:
                click.echo(f"        {health.error}")
            progress.step("tailscale_serve", "warn", health.error)

    return serve_healthy
