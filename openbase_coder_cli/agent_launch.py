"""Build the command line for ``openbase-coder codex|claude`` on this computer.

Plain ``codex`` and ``claude`` are never touched: only these explicit
launchers apply Openbase's session profile, so a user who wants their own
defaults keeps them. The same planning runs in two places:

- in the user's terminal, for a local session (``openbase-coder codex``);
- on an Openbase Sync hub, for a session an edge started there remotely
  (``agent_terminal`` attaches the hub's PTY to the edge's terminal).

Codex: Openbase's profile is ``$CODEX_HOME/openbase.config.toml`` (or
``openbase-cloud.config.toml`` when an Openbase Cloud backend is selected),
selected with ``-p``. A new interactive session attaches to the machine's
Openbase-managed app-server with an explicit ``--remote`` so the dispatcher
and the phone see it and can steer it. Two Codex facts shape the argv
(verified against codex-cli 0.160.1):

- Over ``--remote`` the TUI replays only typed session settings (model,
  reasoning effort, permissions, cwd) onto the shared server. Free-form
  config — from a profile *or* from ``-c`` overrides — is not replayed, so
  MCP servers, hooks and provider definitions come from the server's own
  configuration. ``-c`` overrides are therefore no better than ``-p``.
- Without ``-C`` a remote TUI's new thread runs in the *server's* working
  directory, so the launcher always passes the user's cwd explicitly.

The Openbase Cloud profile names a model provider that only exists in that
profile, which the shared server cannot resolve ("Model provider
`openbase_cloud` not found"), so Cloud sessions run standalone.

Claude Code: Openbase's settings and MCP overrides
(``~/.openbase/profiles/claude/{settings,mcp}.json``) are passed with
``--settings`` / ``--mcp-config``. The settings layer carries the
SessionStart hook that records the session's inbox socket, which is what
makes a terminal Claude session steerable by Openbase. The rendered Openbase
base instructions are appended to the system prompt exactly as Super Agents
does for the sessions it starts.
"""

from __future__ import annotations

import dataclasses
import os
import socket
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from openbase_coder_cli.backend_config import (
    OPENBASE_CLOUD_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)
from openbase_coder_cli.paths import (
    CLAUDE_PROFILE_MCP_PATH,
    CLAUDE_PROFILE_SETTINGS_PATH,
    CODEX_HOME_DIR,
    OPENBASE_AGENTS_MD_PATH,
)

CODEX = "codex"
CLAUDE = "claude"
AGENTS = (CODEX, CLAUDE)

CODEX_PROFILE = "openbase"
CLOUD_CODEX_PROFILE = "openbase-cloud"
CLOUD_CODEX_API_KEY_ENV = "OPENBASE_CLOUD_CODEX_API_KEY"
BASE_INSTRUCTIONS_PATH_ENV = "SUPER_AGENTS_BASE_INSTRUCTIONS_PATH"

# Codex subcommands that open the interactive TUI on an existing thread; they
# take --remote after the subcommand name. Every other subcommand runs as-is
# (with the profile) on this computer.
CODEX_TUI_SUBCOMMANDS = frozenset({"resume", "fork"})
CODEX_OTHER_SUBCOMMANDS = frozenset(
    {
        "a",
        "agents",
        "app",
        "app-server",
        "apply",
        "archive",
        "cloud",
        "completion",
        "debug",
        "delete",
        "doctor",
        "e",
        "exec",
        "exec-server",
        "features",
        "help",
        "login",
        "logout",
        "mcp",
        "mcp-server",
        "migrate-rollouts",
        "plugin",
        "queue",
        "remote-control",
        "review",
        "sandbox",
        "unarchive",
        "update",
    }
)
# Claude Code subcommands that manage the installation rather than run a
# session; they run plain, without Openbase's session layer.
CLAUDE_SUBCOMMANDS = frozenset(
    {
        "agents",
        "attach",
        "auth",
        "auto-mode",
        "doctor",
        "gateway",
        "import",
        "install",
        "kill",
        "logs",
        "mcp",
        "migrate-installer",
        "plugin",
        "plugins",
        "project",
        "respawn",
        "rm",
        "setup-token",
        "stop",
        "ultrareview",
        "update",
        "upgrade",
    }
)

# Identity markers of an enclosing agent session (e.g. `openbase-coder
# claude` typed into a Claude Code shell). Inherited, they make the new TUI
# believe it is a nested child: Claude Code then stops saving the transcript,
# so the session would never show up in Openbase.
INHERITED_SESSION_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_EFFORT",
    "CLAUDE_PID",
    "CODEX_THREAD_ID",
    "CODEX_SANDBOX",
    "CODEX_SANDBOX_NETWORK_DISABLED",
)


class AgentLaunchError(RuntimeError):
    """The agent cannot be launched (with a message meant for the user)."""


@dataclasses.dataclass(frozen=True)
class AgentLaunch:
    agent: str
    argv: list[str]
    cwd: str
    env: dict[str, str]
    # One-line explanations printed before the session starts (for example
    # why a Codex session runs standalone instead of on the shared server).
    notices: tuple[str, ...] = ()
    # Whether the session is visible to Openbase (dispatcher, phone, console).
    attached: bool = True


@dataclasses.dataclass(frozen=True)
class LaunchContext:
    """Machine facts the planner needs, injectable for tests."""

    backend: str
    find_binary: Callable[[str], Path | None]
    codex_endpoint: Callable[[], str | None]
    codex_home: Path = CODEX_HOME_DIR
    claude_settings_path: Path = CLAUDE_PROFILE_SETTINGS_PATH
    claude_mcp_path: Path = CLAUDE_PROFILE_MCP_PATH
    base_instructions_path: Path | None = None
    cloud_codex_api_key: Callable[[], str | None] = lambda: None
    cloud_claude_env: Callable[[str], dict[str, str]] = lambda backend: {}
    cloud_claude_model: Callable[[str | None, str], str | None] = (
        lambda model, backend: model
    )


def is_cloud_backend(backend: str) -> bool:
    return backend in (OPENBASE_CLOUD_BACKEND, OPENBASE_CLOUD_CODEX_BACKEND)


def codex_profile_name(backend: str) -> str:
    return CLOUD_CODEX_PROFILE if is_cloud_backend(backend) else CODEX_PROFILE


def session_env(base: Mapping[str, str]) -> dict[str, str]:
    env = dict(base)
    for name in INHERITED_SESSION_ENV:
        env.pop(name, None)
    return env


def _has_flag(args: Sequence[str], *names: str) -> bool:
    for arg in args:
        if arg == "--":
            return False
        for name in names:
            if arg == name or (name.startswith("--") and arg.startswith(f"{name}=")):
                return True
            # Short options with an attached value (``-Cdir``).
            if len(name) == 2 and arg.startswith(name) and len(arg) > 2:
                return True
    return False


def codex_session_kind(args: Sequence[str]) -> str:
    """``tui`` (new session), ``tui-subcommand`` (resume/fork) or ``other``."""
    if args and args[0] in CODEX_TUI_SUBCOMMANDS:
        return "tui-subcommand"
    if args and args[0] in CODEX_OTHER_SUBCOMMANDS:
        return "other"
    return "tui"


def claude_session_kind(args: Sequence[str]) -> str:
    """``session`` (anything that runs a conversation) or ``other``."""
    if args and args[0] in CLAUDE_SUBCOMMANDS:
        return "other"
    return "session"


def is_interactive_session(agent: str, args: Sequence[str]) -> bool:
    """Whether the invocation is a session that may run on a hub."""
    if agent == CODEX:
        if _has_flag(args, "--remote", "--no-daemon"):
            return False
        return codex_session_kind(args) != "other"
    return claude_session_kind(args) == "session"


def plan_agent_launch(
    agent: str,
    args: Sequence[str],
    cwd: str | Path,
    context: LaunchContext,
    *,
    base_env: Mapping[str, str],
) -> AgentLaunch:
    if agent == CODEX:
        return _plan_codex(list(args), str(cwd), context, base_env)
    if agent == CLAUDE:
        return _plan_claude(list(args), str(cwd), context, base_env)
    raise AgentLaunchError(f"Unknown agent: {agent}")


def _require_binary(context: LaunchContext, name: str, label: str) -> str:
    binary = context.find_binary(name)
    if binary is None:
        raise AgentLaunchError(f"{label} CLI is not installed on this computer.")
    return str(binary)


def _plan_codex(
    args: list[str], cwd: str, context: LaunchContext, base_env: Mapping[str, str]
) -> AgentLaunch:
    binary = _require_binary(context, "codex", "Codex")
    profile = codex_profile_name(context.backend)
    profile_path = context.codex_home / f"{profile}.config.toml"
    if not profile_path.is_file():
        raise AgentLaunchError(
            f"Openbase's Codex profile is missing ({profile_path}). "
            "Run `openbase-coder profiles install` and try again."
        )
    env = session_env(base_env)
    notices: list[str] = []
    cloud = is_cloud_backend(context.backend)
    if cloud and not env.get(CLOUD_CODEX_API_KEY_ENV):
        token = context.cloud_codex_api_key()
        if token:
            env[CLOUD_CODEX_API_KEY_ENV] = token
        else:
            notices.append(
                "Could not get an Openbase Cloud token; run `openbase-coder "
                "login` if Codex reports a missing API key."
            )

    kind = codex_session_kind(args)
    own_endpoint = _has_flag(args, "--remote", "--no-daemon")
    argv = [binary, "-p", profile]
    attached = False
    if kind == "other" or own_endpoint:
        argv += args
    elif cloud:
        notices.append(
            "Openbase Cloud models can't run on the shared Codex app-server "
            "from a terminal yet; starting a standalone session (not visible "
            "to the phone)."
        )
        argv += args
    else:
        endpoint = context.codex_endpoint()
        if endpoint is None:
            notices.append(
                "Openbase's Codex app-server is not running; starting a "
                "standalone session (not visible to the phone)."
            )
            argv += args
        elif kind == "tui-subcommand":
            # resume/fork keep the thread's own cwd on the server.
            argv += [args[0], "--remote", endpoint, *args[1:]]
            attached = True
        else:
            argv += ["--remote", endpoint]
            if not _has_flag(args, "-C", "--cd"):
                argv += ["-C", cwd]
            argv += args
            attached = True
    return AgentLaunch(CODEX, argv, cwd, env, tuple(notices), attached)


def _base_instructions(context: LaunchContext) -> str | None:
    path = context.base_instructions_path
    if path is None:
        return None
    try:
        text = path.expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _plan_claude(
    args: list[str], cwd: str, context: LaunchContext, base_env: Mapping[str, str]
) -> AgentLaunch:
    binary = _require_binary(context, "claude", "Claude Code")
    env = session_env(base_env)
    if claude_session_kind(args) == "other":
        return AgentLaunch(CLAUDE, [binary, *args], cwd, env, (), False)
    for path in (context.claude_settings_path, context.claude_mcp_path):
        if not path.is_file():
            raise AgentLaunchError(
                f"Openbase's Claude Code profile is missing ({path}). "
                "Run `openbase-coder profiles install` and try again."
            )
    # Openbase runs Claude Code on the user's Claude login, never an API key
    # inherited from the shell (mirrors Super Agents' SDK environment).
    env.pop("ANTHROPIC_API_KEY", None)
    argv = [
        binary,
        "--settings",
        str(context.claude_settings_path),
        "--mcp-config",
        str(context.claude_mcp_path),
    ]
    instructions = _base_instructions(context)
    if instructions and not _has_flag(
        args, "--append-system-prompt", "--append-system-prompt-file"
    ):
        argv += ["--append-system-prompt", instructions]
    if is_cloud_backend(context.backend):
        cloud_backend = OPENBASE_CLOUD_BACKEND
        env.update(context.cloud_claude_env(cloud_backend))
        if not _has_flag(args, "--model"):
            model = context.cloud_claude_model(None, cloud_backend)
            if model:
                argv += ["--model", model]
    argv += args
    return AgentLaunch(CLAUDE, argv, cwd, env, (), True)


# --- the machine's real facts --------------------------------------------


def managed_codex_endpoint() -> str | None:
    """The managed app-server endpoint when it accepts connections, else None."""
    from openbase_coder_cli.codex_control_plane import (
        codex_app_server_ready,
        managed_codex_app_server_endpoint,
    )

    endpoint = managed_codex_app_server_endpoint()
    if endpoint.is_unix:
        path = endpoint.socket_path
        if path is None or not unix_socket_accepts(path):
            return None
        return endpoint.value
    return endpoint.value if codex_app_server_ready(endpoint) else None


def unix_socket_accepts(path: Path, timeout: float = 0.5) -> bool:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
    except OSError:
        return False
    finally:
        client.close()
    return True


def _cloud_codex_api_key() -> str | None:
    from openbase_coder_cli.cloud_environment import configured_web_backend_url
    from openbase_coder_cli.config.machine_token_manager import MachineTokenManager

    try:
        return MachineTokenManager(configured_web_backend_url()).get_machine_token()
    except Exception:  # noqa: BLE001 - best effort; Codex reports the gap itself
        return None


def _cloud_claude_env(backend: str) -> dict[str, str]:
    from super_agents.claude_options import openbase_cloud_claude_env

    try:
        return openbase_cloud_claude_env(backend)
    except RuntimeError as exc:
        raise AgentLaunchError(str(exc)) from exc


def _cloud_claude_model(model: str | None, backend: str) -> str | None:
    from super_agents.claude_options import openbase_cloud_claude_model

    return openbase_cloud_claude_model(model, backend)


def selected_backend() -> str:
    from openbase_coder_cli.backend_config import (
        CODING_BACKEND_ENV_KEY,
        DEFAULT_CODING_BACKEND,
        normalize_backend,
    )
    from openbase_coder_cli.env_file import selected_backend_from_env_file
    from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH

    if DEFAULT_ENV_FILE_PATH.is_file():
        return selected_backend_from_env_file(DEFAULT_ENV_FILE_PATH)
    try:
        return normalize_backend(os.environ.get(CODING_BACKEND_ENV_KEY))
    except ValueError:
        return DEFAULT_CODING_BACKEND


def default_launch_context() -> LaunchContext:
    from openbase_coder_cli.backend_binaries import find_backend_binary

    configured = os.environ.get(BASE_INSTRUCTIONS_PATH_ENV, "").strip()
    instructions = Path(configured) if configured else OPENBASE_AGENTS_MD_PATH
    return LaunchContext(
        backend=selected_backend(),
        find_binary=find_backend_binary,
        codex_endpoint=managed_codex_endpoint,
        base_instructions_path=instructions,
        cloud_codex_api_key=_cloud_codex_api_key,
        cloud_claude_env=_cloud_claude_env,
        cloud_claude_model=_cloud_claude_model,
    )
