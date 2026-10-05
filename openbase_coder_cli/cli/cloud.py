"""Openbase Cloud workspace integration commands.

Includes the idle heartbeat and EC2 workspace auth repair. A workspace launched
by openbase-cloud reports whether agent runs made recent progress during the
beat window, so the cloud API can defer idle auto-stop. On every EC2 boot, the
workspace also rehydrates missing/stale Cloud auth by proving its instance
identity to the Cloud API.
"""

from __future__ import annotations

import time

import click
import httpx

from openbase_coder_cli.cli.backend import read_env_values
from openbase_coder_cli.cli.local_server import local_server_url
from openbase_coder_cli.config.machine_token_manager import MachineTokenManager
from openbase_coder_cli.config.token_manager import (
    DEFAULT_WEB_BACKEND_URL,
    AuthLoginRequiredError,
    AuthTransientError,
    CloudAccessTokenAuth,
    TokenManager,
)
from openbase_coder_cli.env_file import upsert_env_file_values
from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH
from openbase_coder_cli.services.cloud_registration import register_and_report
from openbase_coder_cli.services.ec2_identity import (
    EC2IdentityError,
    build_instance_rehydrate_payload,
)

HEARTBEAT_PATH = "/api/openbase/devspaces/heartbeat/"
REHYDRATE_PATH = "/api/openbase/devspaces/instance-auth/rehydrate/"
THREAD_ACTIVITY_PATH = "/api/threads/activity/"

# Runs shorter than one heartbeat interval would be invisible to a single
# per-beat check, so activity is sampled more often and OR-ed into the beat.
RUN_SAMPLE_INTERVAL_SECONDS = 15


def _web_backend_url() -> str:
    if DEFAULT_ENV_FILE_PATH.is_file():
        url = read_env_values(DEFAULT_ENV_FILE_PATH).get(
            "OPENBASE_CODER_CLI_WEB_BACKEND_URL"
        )
        if url:
            return url.rstrip("/")
    return DEFAULT_WEB_BACKEND_URL


def _cloud_bearer_token(manager: TokenManager) -> str:
    """Cloud credential for this installation: the owner's login on a desktop
    or EC2 workspace; the cached scoped machine token on a container
    workspace, which never holds a login."""
    try:
        return manager.get_access_token()
    except AuthLoginRequiredError:
        from openbase_coder_cli.config.machine_token_manager import MachineTokenManager

        machine_tokens = MachineTokenManager(manager.web_backend_url)
        if not machine_tokens.has_cached_token():
            raise
        return machine_tokens.get_machine_token()


def _local_api_auth(manager: TokenManager):
    """Credential for the local coder server. Without an owner login (container
    workspaces) the installation's own local capability token is used."""
    try:
        manager.get_access_token()
    except AuthLoginRequiredError:
        from openbase_coder_cli.config.local_api_token import get_local_api_token

        token = get_local_api_token()
        return httpx.Auth() if not token else _BearerAuth(token)
    except AuthTransientError:
        pass
    return CloudAccessTokenAuth(manager)


class _BearerAuth(httpx.Auth):
    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


def _agent_runs_active(local_url: str, manager: TokenManager) -> bool:
    """True when the workspace is busy: a coder thread reports recent
    productive run activity, or a voice call is live.

    The local coder server is the source of truth for Super Agents / Codex /
    Claude Code runs and for LiveKit rooms. Any failure here (server down,
    auth hiccup) just means "not busy" for this sample; the next sample retries.
    """
    try:
        response = httpx.get(
            f"{local_url}{THREAD_ACTIVITY_PATH}",
            auth=_local_api_auth(manager),
            timeout=10,
        )
    except (httpx.HTTPError, RuntimeError, AuthLoginRequiredError):
        return False
    if response.status_code != 200:
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    if "active" in data:
        return bool(data["active"])
    return int(data.get("active_run_count", 0)) > 0


@click.group()
def cloud() -> None:
    """Openbase Cloud workspace integration."""


@cloud.command()
@click.option(
    "--interval",
    type=int,
    default=60,
    show_default=True,
    help="Seconds between heartbeats. 0 sends a single heartbeat and exits.",
)
def heartbeat(interval: int) -> None:
    """Report agent-run activity to Openbase Cloud so idle auto-stop is deferred."""
    url = _web_backend_url()
    manager = TokenManager(web_backend_url=url)
    manager.load()
    local_url = local_server_url()

    active = _agent_runs_active(local_url, manager)
    while True:
        # A long-running service must survive network blips and token-refresh
        # hiccups: skipping one beat and retrying next interval is the fallback.
        try:
            token = _cloud_bearer_token(manager)
            httpx.post(
                f"{url}{HEARTBEAT_PATH}",
                json={"active": active},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            click.echo(f"Heartbeat skipped: {exc}", err=True)
            if interval <= 0:
                raise
        if interval <= 0:
            break

        # Sample run activity through the beat window so short runs launched
        # between beats still mark the workspace active.
        active = False
        deadline = time.monotonic() + interval
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(RUN_SAMPLE_INTERVAL_SECONDS, remaining))
            active = _agent_runs_active(local_url, manager) or active


@cloud.command("rehydrate-auth")
@click.option(
    "--force",
    is_flag=True,
    help="Request fresh Cloud workspace auth even if the current token is valid.",
)
def rehydrate_auth(force: bool) -> None:
    """Repair Cloud auth on an EC2 workspace boot/resume."""
    url = _web_backend_url()
    manager = TokenManager(web_backend_url=url)
    if not force and _stored_auth_is_valid(manager):
        click.echo("Cloud auth is already valid.")
        return

    try:
        request_payload = build_instance_rehydrate_payload()
    except EC2IdentityError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        response = httpx.post(
            f"{url}{REHYDRATE_PATH}",
            json=request_payload,
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"Cloud auth rehydrate request failed: {exc}"
        ) from exc
    if response.status_code >= 400:
        raise click.ClickException(
            f"Cloud auth rehydrate was rejected with HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    payload = response.json()
    access_token = str(payload.get("access_token") or "")
    refresh_token = str(payload.get("refresh_token") or "")
    if not access_token or not refresh_token:
        raise click.ClickException("Cloud auth rehydrate response was missing tokens.")
    web_backend_url = str(payload.get("web_backend_url") or url).rstrip("/")
    expires_at = payload.get("access_expires_at")
    expires_in = max(int(expires_at - time.time()), 60) if expires_at else 300

    if web_backend_url != url:
        url = web_backend_url
        manager = TokenManager(web_backend_url=url)
    DEFAULT_ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    upsert_env_file_values(
        DEFAULT_ENV_FILE_PATH,
        {"OPENBASE_CODER_CLI_WEB_BACKEND_URL": url},
    )
    manager.store_tokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )
    MachineTokenManager(url, manager).get_machine_token(rotate=True)
    report = register_and_report()
    if not report.ok and report.supported:
        click.echo(
            f"Warning: Cloud auth was repaired, but device registration failed: {report.error}",
            err=True,
        )
    click.echo("Cloud auth rehydrated.")


def _stored_auth_is_valid(manager: TokenManager) -> bool:
    try:
        manager.get_access_token()
    except AuthLoginRequiredError:
        return False
    except AuthTransientError as exc:
        raise click.ClickException(
            f"Could not verify stored Cloud auth: {exc}"
        ) from exc
    return True
