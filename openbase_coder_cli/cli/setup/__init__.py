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
from click.core import ParameterSource

from openbase_coder_cli.backend_binaries import ensure_backend_binary
from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    CODING_BACKEND_ENV_KEY,  # noqa: F401
    DEFAULT_CODING_BACKEND,  # noqa: F401
    OPENBASE_CLOUD_BACKEND,
    SELECTABLE_BACKENDS,
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
    AUDIO_PROVIDER_CARTESIA,
    AUDIO_PROVIDER_LOCAL,
    AUDIO_PROVIDER_OPENBASE_CLOUD,
    AUDIO_PROVIDER_OPTIONS,
    CODEX_HOME_DEFAULT_DISPATCHER_CONFIG,  # noqa: F401
    DEFAULT_AUDIO_PROVIDER,
    LOCAL_AUDIO_PYTHON_MAX,  # noqa: F401
    LOCAL_AUDIO_REQUIREMENTS,  # noqa: F401
    _audio_provider_config,  # noqa: F401
    _default_dispatcher_config,  # noqa: F401
    _download_local_audio_models,
    _ensure_codex_home_dispatcher_config,
    _ensure_local_audio_dependencies,
    _local_audio_dependencies_available,  # noqa: F401
    _python_version,  # noqa: F401
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
from openbase_coder_cli.cli.setup.hooks import (
    ensure_session_id_hook_script as _ensure_session_id_hook_script,
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
    DEFAULT_WEB_BACKEND_URL,
    AuthLoginRequiredError,  # noqa: F401
    AuthTransientError,  # noqa: F401
    TokenManager,
)
from openbase_coder_cli.dispatcher_config import (
    DISPATCHER_VOICE_ID_KEY,  # noqa: F401
    DISPATCHER_VOICE_NAME_KEY,  # noqa: F401
    STT_PROVIDER_KEY,  # noqa: F401
    TTS_PROVIDER_KEY,  # noqa: F401
    set_dispatcher_service_tier,
)
from openbase_coder_cli.livekit_install import ensure_pinned_livekit_server
from openbase_coder_cli.paths import (
    CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH,  # noqa: F401
    CODEX_DISPATCHER_CONFIG_PATH,
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
from openbase_coder_cli.services.cloud_registration import register_and_report
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

CODING_BACKEND_OPTIONS = SELECTABLE_BACKENDS
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
    help=("Symlink Openbase's Claude settings to the normal ~/.claude/settings.json."),
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
        "Interactive runs pick it when creating a new env file if omitted; "
        "non-interactive fresh installs require it. Existing env files are "
        "only changed when this option is provided."
    ),
)
@click.option(
    "--audio-provider",
    type=click.Choice(AUDIO_PROVIDER_OPTIONS),
    default=None,
    help=(
        "Voice audio provider. Interactive runs pick it for new dispatcher "
        "configs if omitted; otherwise new configs use openbase-cloud. "
        "Existing configs are only changed when this option is provided."
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
@click.option(
    "--interactive/--non-interactive",
    "interactive_mode",
    default=None,
    help=(
        "Force or forbid the first-run pickers (coding backend, voice audio "
        "provider, BYOK voice keys). By default setup is only interactive "
        "when run with no flags at all on a terminal; passing any flag "
        "implies --non-interactive, so scripted and AI-agent runs never "
        "block on a prompt. Non-interactive fresh installs require "
        "--backend and default the audio provider to openbase-cloud."
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
    interactive_mode: bool | None,
) -> None:
    """Full install flow for Openbase Coder.

    Run with no flags on a terminal for an interactive first-time setup with
    pickers for the coding backend and voice audio provider. Passing any flag
    disables all prompts (AI-agent and script safe): fresh installs then
    require --backend and default the audio provider to openbase-cloud. Pass
    --interactive to combine flags with the pickers.
    """
    if platform.system() not in ("Darwin", "Linux", "Windows"):
        raise click.ClickException("Setup is only supported on macOS and Linux.")
    interactive = _resolve_interactive_mode(interactive_mode, json_progress)
    if coding_backend is not None:
        try:
            coding_backend = normalize_backend(coding_backend)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    coding_backend = _require_backend_choice(
        env_file, coding_backend, interactive=interactive
    )
    audio_provider = _require_audio_provider_choice(
        audio_provider, interactive=interactive
    )
    assembly_ai_api_key, cartesia_api_key = _require_byok_audio_keys(
        env_file,
        audio_provider,
        assembly_ai_api_key,
        cartesia_api_key,
        interactive=interactive,
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
    if interactive:
        _interactive_cloud_login_and_checks(env_file, cli_configured=cli_configured)
        _print_app_download_qr()
    else:
        click.echo(
            "To enable remote authentication, run 'openbase-coder login' "
            "and ensure OPENBASE_CODER_CLI_WEB_BACKEND_URL is set in your .env."
        )


APP_DOWNLOADS_URL = "https://openbase.cloud/downloads.html"


def _interactive_cloud_login_and_checks(env_file: str, *, cli_configured: bool) -> None:
    """Interactive setup tail: login, then verify cloud registration and Serve.

    Only ever called on interactive runs; non-interactive runs (including the
    desktop app's --json-progress onboarding, which renders its own sign-in
    step) keep the plain login hint instead.
    """
    web_backend_url = (
        _env_file_values(Path(env_file)).get("OPENBASE_CODER_CLI_WEB_BACKEND_URL")
        or DEFAULT_WEB_BACKEND_URL
    )
    if TokenManager(web_backend_url).has_refresh_token:
        click.echo("Already logged in to Openbase Cloud.")
    elif click.confirm(
        "Log in to Openbase Cloud now? (required for iPhone pairing and "
        "cloud onboarding)",
        default=True,
    ):
        from openbase_coder_cli.cli import auth as _auth

        click.get_current_context().invoke(_auth.login)
    else:
        click.echo(
            "Skipping login. Run 'openbase-coder login' later; iPhone pairing "
            "and cloud onboarding need it."
        )
        return

    # Login already registers the device; re-report with the freshest facts
    # so the cloud sees this install as configured, and surface the result.
    serve_health = tailscale_serve_health()
    report = register_and_report(
        cli_configured=cli_configured,
        serve_healthy=serve_health.healthy,
    )
    if report.ok:
        click.echo("Device registered with Openbase Cloud.")
    elif report.supported:
        click.echo(
            click.style(
                "Warning: could not register this device with Openbase "
                f"Cloud: {report.error}",
                fg="yellow",
            )
        )
    if serve_health.healthy:
        click.echo("Tailscale Serve is exposing the local API and LiveKit.")
    else:
        click.echo(
            click.style(
                "Warning: Tailscale Serve is not fully healthy: "
                f"{serve_health.error or 'routes not configured'}",
                fg="yellow",
            )
        )
        click.echo(
            "  Re-check with 'openbase-coder onboarding status' once "
            "Tailscale is signed in and connected."
        )


def _print_app_download_qr() -> None:
    """Terminal QR code pointing at the phone app downloads page."""
    import qrcode

    click.echo()
    click.echo("Scan to get the Openbase iOS/Android app:")
    qr = qrcode.QRCode(border=1)
    qr.add_data(APP_DOWNLOADS_URL)
    qr.print_ascii(invert=True)
    click.echo(APP_DOWNLOADS_URL)


def _resolve_interactive_mode(
    interactive_mode: bool | None,
    json_progress: bool,
) -> bool:
    """Decide whether setup may prompt.

    Interactive only when explicitly forced with --interactive, or when the
    command was invoked with no command-line flags at all on a terminal.
    Any explicit flag implies non-interactive so scripted and AI-agent runs
    never block on a prompt.
    """
    if json_progress:
        return False
    if interactive_mode is not None:
        return interactive_mode
    ctx = click.get_current_context(silent=True)
    if ctx is not None and any(
        ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE
        for name in ctx.params
    ):
        return False
    return sys.stdin.isatty()


def _prompt_pick(
    title: str,
    options: tuple[tuple[str, str, str], ...],
    *,
    default: str | None = None,
) -> str:
    """Numbered terminal picker over (value, label, description) options."""
    click.echo()
    click.echo(title)
    default_number: int | None = None
    for number, (value, label, description) in enumerate(options, start=1):
        click.echo(f"  {number}) {label} — {description}")
        if value == default:
            default_number = number
    choice = click.prompt(
        "Choose an option",
        type=click.IntRange(1, len(options)),
        default=default_number,
        show_default=default_number is not None,
    )
    return options[choice - 1][0]


_BACKEND_PICKER_OPTIONS = (
    (
        CODEX_BACKEND,
        "codex",
        "native Codex app-server with OpenAI models, using your Codex CLI login",
    ),
    (
        CLAUDE_CODE_BACKEND,
        "claude-code",
        "Claude Code using your local Claude login and billing",
    ),
    (
        OPENBASE_CLOUD_BACKEND,
        "openbase-cloud",
        "Cloud-proxied Claude Code with only an Openbase login; no personal "
        "Anthropic account needed",
    ),
)

_AUDIO_PROVIDER_PICKER_OPTIONS = (
    (
        AUDIO_PROVIDER_OPENBASE_CLOUD,
        "Cloud TTS/STT",
        "managed speech-to-text and text-to-speech through Openbase Cloud "
        "(recommended)",
    ),
    (
        AUDIO_PROVIDER_CARTESIA,
        "Bring your own keys",
        "AssemblyAI speech-to-text and Cartesia text-to-speech with your own API keys",
    ),
    (
        AUDIO_PROVIDER_LOCAL,
        "Local models",
        "on-device Kokoro TTS and MLX Whisper STT; Apple Silicon with Python "
        "3.12 only (not recommended)",
    ),
)


def _require_backend_choice(
    env_file: str,
    coding_backend: str | None,
    *,
    interactive: bool,
) -> str | None:
    """Resolve the backend for a fresh install without preferring one.

    Existing env files keep their configured backend. New installs must pick
    one: interactively via a picker, otherwise via --backend.
    """
    if coding_backend is not None or Path(env_file).is_file():
        return coding_backend
    if interactive:
        choice = _prompt_pick("Coding backend:", _BACKEND_PICKER_OPTIONS)
        return normalize_backend(choice)
    raise click.ClickException(
        "No coding backend configured yet. Pass --backend "
        "codex|claude-code|openbase-cloud for a first-time setup."
    )


def _require_audio_provider_choice(
    audio_provider: str | None,
    *,
    interactive: bool,
) -> str | None:
    """Pick the voice audio provider for a fresh install.

    Existing dispatcher configs keep their configured provider. Fresh
    interactive installs pick one; non-interactive installs keep the
    openbase-cloud default.
    """
    if audio_provider is not None or CODEX_DISPATCHER_CONFIG_PATH.exists():
        return audio_provider
    if interactive:
        return _prompt_pick(
            "Voice audio provider:",
            _AUDIO_PROVIDER_PICKER_OPTIONS,
            default=DEFAULT_AUDIO_PROVIDER,
        )
    return audio_provider


def _require_byok_audio_keys(
    env_file: str,
    audio_provider: str | None,
    assembly_ai_api_key: str,
    cartesia_api_key: str,
    *,
    interactive: bool,
) -> tuple[str, str]:
    """Collect voice keys when the bring-your-own-keys provider is picked.

    Setup only writes these keys into a freshly generated env file, so an
    existing env file is left for the user to edit by hand instead.
    """
    if (
        audio_provider != AUDIO_PROVIDER_CARTESIA
        or Path(env_file).is_file()
        or not interactive
    ):
        return assembly_ai_api_key, cartesia_api_key
    if not assembly_ai_api_key:
        assembly_ai_api_key = click.prompt("AssemblyAI API key (speech-to-text)")
    if not cartesia_api_key:
        cartesia_api_key = click.prompt("Cartesia API key (text-to-speech)")
    return assembly_ai_api_key, cartesia_api_key


def _refuse_to_clobber_dev_install() -> None:
    """Mirror of scripts/setup's guard, in the standalone direction.

    A standalone setup over a development-workspace install would rewrite
    installation.json and regenerate every service against the package,
    silently converting the developer's install out from under the workspace.
    """
    if not InstallationConfig.exists():
        return
    try:
        existing = InstallationConfig.load()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return
    if existing.standalone:
        return
    where = (
        f" (workspace at {existing.workspace_path})" if existing.workspace_path else ""
    )
    raise click.ClickException(
        "A development workspace install of Openbase Coder already exists on "
        f"this machine{where}. Refusing to convert it to a standalone "
        "install. Uninstall it first: https://docs.openbase.cloud/uninstall/"
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
    if runtime_package is not None:
        _refuse_to_clobber_dev_install()

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
    if use_dev_workspace:
        # Standalone packages bundle the pinned engine; dev installs download
        # the same pin so both pathways exercise one livekit-server.
        ensure_pinned_livekit_server()
    if selected_coding_backend == CODEX_BACKEND:
        _symlink_codex_auth()
    _ensure_normal_claude_md_symlink()
    _ensure_codex_home_default_files(workspace_dir if use_dev_workspace else "")
    _ensure_codex_home_dispatcher_config(audio_provider=audio_provider)
    set_dispatcher_service_tier("fast" if fast_mode else "standard")
    click.echo(
        f"Voice dispatcher service tier: {'fast' if fast_mode else 'standard'} "
        "(Super Agents: standard; both adjustable in console settings)."
    )
    if audio_provider == AUDIO_PROVIDER_LOCAL:
        _ensure_local_audio_dependencies(runtime_package)
        _download_local_audio_models()
    _symlink_codex_home_skills(workspace_dir if use_dev_workspace else "")

    # --- Initialize runtime assets ---
    if use_dev_workspace:
        _init_cli_workspace(workspace_dir)
    else:
        _init_standalone_runtime(runtime_package)

    # --- Configure the service CODEX_HOME ---
    _ensure_session_id_hook_script()
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
    # UI-driven setup (--json-progress) must never block on an interactive
    # browser OAuth flow; the desktop app renders a dedicated backend sign-in
    # step after setup instead (see specs/onboarding).
    _ensure_claude_auth_bridge(
        login_if_needed=selected_coding_backend == CLAUDE_CODE_BACKEND
        and not progress.enabled,
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
    elif runtime_package.console_build_dir.is_dir():
        click.echo(
            f"Using bundled console build at {runtime_package.console_build_dir}"
        )
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
        if not skip_services:
            # Tailscale is a hard prerequisite: without it the phone cannot
            # reach this machine, so a "successful" setup would be broken.
            # progress.abort() reports this step as errored on the way out.
            raise click.ClickException(
                f"Tailscale Serve could not be configured: {exc}\n"
                "Tailscale is required — install it, sign in, and connect "
                "(https://tailscale.com/download), then re-run "
                "'openbase-coder setup'."
            ) from exc
        # --skip-services installs (e.g. image bakes) configure Tailscale on
        # first boot instead; leave the manual commands as a breadcrumb.
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
