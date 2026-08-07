"""Env phase: generate and update the Openbase Coder .env file."""

from __future__ import annotations

import secrets
from pathlib import Path

import click

from openbase_coder_cli.backend_config import (
    CODING_BACKEND_ENV_KEY,
    DEFAULT_CODING_BACKEND,
    normalize_backend,
)
from openbase_coder_cli.config.machine_token_manager import (
    MachineTokenError,
    MachineTokenManager,
)
from openbase_coder_cli.config.token_manager import (
    DEFAULT_WEB_BACKEND_URL,
    AuthLoginRequiredError,
    AuthTransientError,
    TokenManager,
)
from openbase_coder_cli.env_file import (
    env_file_values as _env_file_values,
)
from openbase_coder_cli.env_file import (
    selected_backend_from_env_file,
)
from openbase_coder_cli.env_file import (
    upsert_env_file_values as _upsert_env_file_values,
)
from openbase_coder_cli.paths import (
    CODEX_DISPATCHER_CONFIG_PATH,
    OPENBASE_CLAUDE_CONFIG_DIR,
)


def _ensure_env_file(
    env_file: str,
    *,
    assembly_ai_api_key: str,
    cartesia_api_key: str,
    coding_backend: str | None = None,
) -> None:
    path = Path(env_file)
    if coding_backend:
        coding_backend = normalize_backend(coding_backend)
    if path.is_file():
        updates = _missing_livekit_client_credential_values(path)
        if coding_backend:
            updates[CODING_BACKEND_ENV_KEY] = coding_backend
        if updates:
            _upsert_env_file_values(path, updates)
            if coding_backend:
                click.echo(f"Updated {CODING_BACKEND_ENV_KEY} in {path}.")
            if any(key.startswith("LIVEKIT_CLIENT_") for key in updates):
                click.echo(
                    f"Updated client-facing LiveKit token credentials in {path}."
                )
            return
        click.echo(f".env already exists at {path}, leaving unchanged.")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    secret_key = secrets.token_urlsafe(50)
    livekit_api_key = "APIkey" + secrets.token_urlsafe(12)
    livekit_api_secret = secrets.token_urlsafe(32)
    livekit_client_api_key = "APIkey" + secrets.token_urlsafe(12)
    livekit_client_api_secret = secrets.token_urlsafe(32)

    lines = [
        f"OPENBASE_CODER_CLI_SECRET_KEY={secret_key}",
        "# Local server/admin credentials. Do not return these in client API responses.",
        f"LIVEKIT_API_KEY={livekit_api_key}",
        f"LIVEKIT_API_SECRET={livekit_api_secret}",
        "# Client-facing token issuer. LiveKit JWTs expose this key in the issuer claim.",
        f"LIVEKIT_CLIENT_API_KEY={livekit_client_api_key}",
        f"LIVEKIT_CLIENT_API_SECRET={livekit_client_api_secret}",
        "# Use tailscale for phone-to-computer voice calls; use local for loopback-only testing.",
        "LIVEKIT_NETWORK_MODE=tailscale",
        "LIVEKIT_URL=ws://localhost:7880",
        "# In tailscale mode, the managed service rewrites localhost LIVEKIT_URL to the Tailscale IPv4 address.",
        "# The local Python agent still registers over localhost unless LIVEKIT_AGENT_URL is set.",
        "# LIVEKIT_AGENT_URL=ws://localhost:7880",
        "# Override the Tailscale IP LiveKit advertises in ICE candidates.",
        "# If unset in tailscale mode, the managed service uses the first `tailscale ip -4` value.",
        "# LIVEKIT_NODE_IP=100.x.y.z",
        "# Override the Tailscale interface used for LiveKit media.",
        "# If unset, the managed service derives it from LIVEKIT_NODE_IP.",
        "# LIVEKIT_INTERFACE=utun4",
        "# Override the address LiveKit binds locally. Keep this on localhost when using Tailscale Serve.",
        "# LIVEKIT_BIND_IP=127.0.0.1",
        "# Override the LiveKit agent health/control listener. Keep this on localhost.",
        "# LIVEKIT_AGENT_HOST=127.0.0.1",
        "# Override the LiveKit TCP media fallback port.",
        "# LIVEKIT_TCP_PORT=7881",
        "# Override the LiveKit UDP media port.",
        "# LIVEKIT_UDP_PORT=7882",
        "# Override the CLI API listener. Keep this on localhost when using Tailscale Serve.",
        "# OPENBASE_CODER_CLI_HOST=127.0.0.1",
        "# Allow localhost and Tailscale Serve hostnames.",
        "OPENBASE_CODER_CLI_ALLOWED_HOSTS=localhost,127.0.0.1,.ts.net",
        "# Coding backend used by Super Agents and the managed service.",
        f"# Set {CODING_BACKEND_ENV_KEY} to codex, openbase_cloud, or claude_code.",
        f"{CODING_BACKEND_ENV_KEY}={coding_backend or DEFAULT_CODING_BACKEND}",
        "# openbase_cloud runs Cloud-proxied Claude Code; codex uses codex-app-server.",
        f"CLAUDE_CONFIG_DIR={OPENBASE_CLAUDE_CONFIG_DIR}",
        f"SUPER_AGENTS_DEFAULT_CONFIG_PATH={CODEX_DISPATCHER_CONFIG_PATH}",
        "CODEX_MODEL_REASONING_EFFORT=high",
        "# App-server ambient tier follows the Super Agents lane; the voice",
        "# dispatcher passes its (fast by default) tier explicitly per turn.",
        "CODEX_SERVICE_TIER=standard",
        "DISPATCHER_SERVICE_TIER=fast",
        "SUPER_AGENTS_SERVICE_TIER=standard",
        "CODEX_APP_SERVER_URL=ws://127.0.0.1:4500",
        "LIVEKIT_CODEX_THREAD_CWD=~",
        "# Cartesia voice used by the LiveKit agent TTS.",
        "CARTESIA_VOICE_ID=9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
        "OPENBASE_CODER_CLI_OAUTH_CLIENT_ID=openbase-coder-cli",
    ]

    if assembly_ai_api_key:
        lines.append(f"ASSEMBLY_AI_API_KEY={assembly_ai_api_key}")
    if cartesia_api_key:
        lines.append(f"CARTESIA_API_KEY={cartesia_api_key}")

    lines.extend(
        [
            "# Override the web backend URL (defaults to https://app.openbase.cloud):",
            "# OPENBASE_CODER_CLI_WEB_BACKEND_URL=https://app.openbase.cloud",
            "# Override JWT key/session endpoints if your backend routes differ:",
            "# OPENBASE_CODER_CLI_JWT_JWKS_URL=https://app.openbase.cloud/.well-known/jwks.json",
            "# OPENBASE_CODER_CLI_JWT_AUTH_SESSION_URL=https://app.openbase.cloud/_allauth/app/v1/auth/session",
            "# Standalone installs auto-apply updates; set to 0 to opt out.",
            "# OPENBASE_CODER_AUTO_UPDATE=1",
        ]
    )

    path.write_text("\n".join(lines) + "\n")
    click.echo(f"Generated .env at {path}")


def _selected_coding_backend(env_file: Path, requested_backend: str | None) -> str:
    if requested_backend:
        return normalize_backend(requested_backend)
    return selected_backend_from_env_file(env_file)


def _ensure_openbase_cloud_machine_token(env_file: Path) -> None:
    web_backend_url = _env_file_values(env_file).get(
        "OPENBASE_CODER_CLI_WEB_BACKEND_URL",
        DEFAULT_WEB_BACKEND_URL,
    )
    token_manager = TokenManager(web_backend_url)
    if not token_manager.has_refresh_token:
        click.echo(
            "Openbase Cloud backend selected. Run `openbase-coder login` before "
            "starting services so setup can create the cloud proxy machine token."
        )
        return
    try:
        MachineTokenManager(web_backend_url, token_manager).get_machine_token()
    except AuthLoginRequiredError:
        click.echo(
            "Openbase Cloud backend selected, but your Openbase login needs to be "
            "refreshed. Run `openbase-coder login` before starting services."
        )
    except (AuthTransientError, MachineTokenError) as exc:
        click.echo(
            click.style(
                f"Warning: could not create Openbase Cloud machine token: {exc}",
                fg="yellow",
            )
        )
    else:
        click.echo("Openbase Cloud machine token is configured.")


def _missing_livekit_client_credential_values(path: Path) -> dict[str, str]:
    existing = _env_file_values(path)
    updates: dict[str, str] = {}
    if not existing.get("CLAUDE_CONFIG_DIR"):
        updates["CLAUDE_CONFIG_DIR"] = str(OPENBASE_CLAUDE_CONFIG_DIR)
    if not existing.get("SUPER_AGENTS_DEFAULT_CONFIG_PATH"):
        updates["SUPER_AGENTS_DEFAULT_CONFIG_PATH"] = str(CODEX_DISPATCHER_CONFIG_PATH)
    if not existing.get("LIVEKIT_CLIENT_API_KEY") or existing.get(
        "LIVEKIT_CLIENT_API_KEY"
    ) == existing.get("LIVEKIT_API_KEY"):
        updates["LIVEKIT_CLIENT_API_KEY"] = "APIkey" + secrets.token_urlsafe(12)
    if not existing.get("LIVEKIT_CLIENT_API_SECRET") or existing.get(
        "LIVEKIT_CLIENT_API_SECRET"
    ) == existing.get("LIVEKIT_API_SECRET"):
        updates["LIVEKIT_CLIENT_API_SECRET"] = secrets.token_urlsafe(32)
    return updates
