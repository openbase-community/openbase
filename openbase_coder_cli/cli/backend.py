from __future__ import annotations

from pathlib import Path

import click

from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    CODING_BACKEND_ENV_KEY,
    CODING_BACKENDS_ENV_KEY,
    DEFAULT_CODING_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    SELECTABLE_BACKENDS,
    normalize_backend,
)
from openbase_coder_cli.env_file import (
    active_env_key,
    format_env_value,
    upsert_env_file_values,
)
from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH

BACKEND_ENV_KEY = CODING_BACKEND_ENV_KEY


@click.group()
def backend() -> None:
    """View or switch the selected coding backend."""


@backend.command("list")
def list_backends() -> None:
    """List supported coding backends."""
    for backend_name in SELECTABLE_BACKENDS:
        marker = " (default)" if backend_name == DEFAULT_CODING_BACKEND else ""
        click.echo(f"{backend_name}{marker}")


@backend.command()
@click.option(
    "--env-file",
    type=click.Path(path_type=Path),
    default=DEFAULT_ENV_FILE_PATH,
    show_default=True,
    help="Openbase .env file to inspect.",
)
def status(env_file: Path) -> None:
    """Show the currently selected coding backend."""
    value = read_backend(env_file)
    exists = "exists" if env_file.is_file() else "missing"
    click.echo(f"Backend: {value}")
    click.echo(f"Env file: {env_file} ({exists})")


@backend.command("use")
@click.argument("backend_name", nargs=-1)
@click.option(
    "--env-file",
    type=click.Path(path_type=Path),
    default=DEFAULT_ENV_FILE_PATH,
    show_default=True,
    help="Openbase .env file to update.",
)
@click.option(
    "--skip-login-check",
    is_flag=True,
    help="Switch even if the target backend has no usable login.",
)
@click.option(
    "--no-restart",
    is_flag=True,
    help="Persist the choice without restarting the dispatcher services.",
)
def use_backend(
    backend_name: tuple[str, ...],
    env_file: Path,
    skip_login_check: bool,
    no_restart: bool,
) -> None:
    """Switch the coding backend: check its login, persist it, restart the dispatcher."""
    try:
        normalized = normalize_backend(" ".join(backend_name))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not skip_login_check:
        problem = backend_login_problem(normalized)
        if problem is not None:
            raise click.ClickException(
                f"{problem} Backend unchanged; pass --skip-login-check to switch anyway."
            )
    write_backend(env_file, normalized)
    click.echo(f"Backend set to {normalized} in {env_file}.")
    if no_restart:
        click.echo(
            "Restart skipped; the running dispatcher keeps the previous backend "
            "until 'openbase-coder restart --service codex-app-server-dispatcher "
            "--recreate-dispatcher' runs."
        )
        return
    plan = schedule_backend_restart()
    if plan is None:
        click.echo("Openbase services are not installed; nothing to restart.")
    else:
        click.echo(
            "Dispatcher restart scheduled; a fresh dispatcher thread will come up "
            f"on {normalized}. Other Openbase services keep running."
        )


def backend_login_problem(backend_name: str) -> str | None:
    """A human-readable reason the backend cannot be used, or None when logged in."""
    if backend_name == CODEX_BACKEND:
        from openbase_coder_cli.paths import CODEX_HOME_DIR

        if not (CODEX_HOME_DIR / "auth.json").is_file():
            return "Not logged in to Codex: run 'codex login' first."
        return None
    if backend_name == CLAUDE_CODE_BACKEND:
        from openbase_coder_cli.claude_auth import claude_auth_status

        status = claude_auth_status()
        if not status.logged_in:
            detail = f" ({status.raw_output})" if status.raw_output else ""
            return (
                f"Not logged in to Claude Code{detail}: run "
                "'openbase-coder claude login' first."
            )
        return None
    # openbase_cloud and the internal openbase_cloud_codex proxy both ride the
    # Openbase Cloud login.
    from openbase_coder_cli.services.onboarding import cloud_login_status

    login = cloud_login_status()
    if login["status"] == "logged_in":
        return None
    if login["status"] == "login_expired":
        return (
            "Openbase Cloud login expired or was revoked: run "
            "'openbase-coder login' first."
        )
    return "Not logged in to Openbase Cloud: run 'openbase-coder login' first."


def schedule_backend_restart():
    """Restart the dispatcher stack so it serves the newly selected backend.

    Restarting the dispatcher app-server cascades to livekit-agent
    (restart_dependents) and recreates the dispatcher thread, so the next
    voice turn starts a fresh conversation on the new backend. Returns the
    scheduled RestartPlan, or None when there is nothing running to restart.
    """
    from openbase_coder_cli.services.installation import InstallationConfig

    if not InstallationConfig.exists():
        return None
    from openbase_coder_cli.services.launchd import launchctl_status
    from openbase_coder_cli.services.registry import find_service
    from openbase_coder_cli.services.restart import RestartRequest, schedule_restart

    for name in ("codex-app-server-dispatcher", "livekit-agent"):
        if launchctl_status(find_service(name)).get("installed"):
            return schedule_restart(
                RestartRequest(services=(name,), recreate_dispatcher=True)
            )
    return None


def read_backend(env_file: Path) -> str:
    if not env_file.is_file():
        return DEFAULT_CODING_BACKEND
    values = read_env_values(env_file)
    raw_value = values.get(BACKEND_ENV_KEY)
    try:
        return normalize_backend(raw_value)
    except ValueError:
        return f"unsupported:{raw_value}"


def write_backend_location(env_file: Path, location: str) -> None:
    """Materialize a Local-CLI vs Openbase-Cloud choice into the env file.

    The user picks WHERE code runs; the engines are not a user choice. Local
    engages both local engines (mixed threads, model picks the engine per
    launch); Cloud makes the Openbase Cloud proxy primary while local Codex
    threads stay visible (read-only in practice: creation follows the
    primary).
    """
    from openbase_coder_cli.dispatcher_config import (
        LOCATION_CLOUD,
        LOCATION_LOCAL,
        identity_for_model,
        super_agents_model,
    )

    if location not in {LOCATION_LOCAL, LOCATION_CLOUD}:
        raise ValueError("Location must be 'local' or 'cloud'.")
    if location == LOCATION_CLOUD:
        values = {
            BACKEND_ENV_KEY: OPENBASE_CLOUD_BACKEND,
            CODING_BACKENDS_ENV_KEY: f"{OPENBASE_CLOUD_BACKEND},{CODEX_BACKEND}",
        }
    else:
        model = super_agents_model() or ""
        primary = identity_for_model(model, LOCATION_LOCAL) if model else CODEX_BACKEND
        values = {
            BACKEND_ENV_KEY: primary,
            CODING_BACKENDS_ENV_KEY: f"{CODEX_BACKEND},{CLAUDE_CODE_BACKEND}",
        }
    env_file.parent.mkdir(parents=True, exist_ok=True)
    if env_file.is_file():
        upsert_env_file_values(env_file, values)
    else:
        env_file.write_text(
            "".join(
                f"{key}={format_env_value(value)}\n" for key, value in values.items()
            ),
            encoding="utf-8",
        )


def write_backend(env_file: Path, backend_name: str) -> None:
    # Backend model/provider choices reach Codex as app-server launch
    # overrides (services/runners.py), so persisting the env value is enough;
    # the service restart picks it up.
    normalized = normalize_backend(backend_name)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    if env_file.is_file():
        upsert_env_file_values(env_file, {BACKEND_ENV_KEY: normalized})
    else:
        env_file.write_text(
            f"{BACKEND_ENV_KEY}={format_env_value(normalized)}\n", encoding="utf-8"
        )


def read_env_values(env_file: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key = active_env_key(line)
        if key is None:
            continue
        _raw_key, raw_value = line.split("=", 1)
        values[key] = _unquote_env_value(raw_value.strip())
    return values


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
