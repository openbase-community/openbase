"""Non-interactive provisioning for cloud workspaces.

EC2 workspaces consume the existing JSON provisioning bundle. Isolated
container workspaces instead exchange a short-lived, single-use bootstrap
grant for an installation-scoped machine token and Netmesh enrollment key;
they never receive or persist the owner's access or refresh tokens.

Everything here reuses existing helpers; it only sequences them for a headless
boot rather than reimplementing setup.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import click
import httpx

from openbase_coder_cli.config.machine_token_manager import (
    MachineTokenError,
    MachineTokenManager,
)
from openbase_coder_cli.config.token_manager import (
    DEFAULT_WEB_BACKEND_URL,
    TokenManager,
)
from openbase_coder_cli.env_file import env_file_values, upsert_env_file_values
from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH, OWNER_IDENTITY_JSON_PATH

from .setup import setup

WEB_BACKEND_ENV_KEY = "OPENBASE_CODER_CLI_WEB_BACKEND_URL"
BOOTSTRAP_TOKEN_ENV_KEY = "OPENBASE_CODER_BOOTSTRAP_TOKEN"
BOOTSTRAP_EXCHANGE_PATH = "/api/openbase/devspaces/bootstrap/exchange/"
NETMESH_AUTHKEY_FILE = DEFAULT_ENV_FILE_PATH.parent / "bootstrap-netmesh-authkey"


def _load_bundle(input_file: str | None, overrides: dict) -> dict:
    bundle: dict = {}
    if input_file:
        bundle = json.loads(Path(input_file).read_text(encoding="utf-8"))
    # Explicit flags win over the file so the command is also usable by hand.
    for key, value in overrides.items():
        if value:
            bundle[key] = value
    return bundle


def _store_auth(bundle: dict, web_backend_url: str) -> None:
    access_token = bundle.get("access_token", "")
    refresh_token = bundle.get("refresh_token", "")
    if not access_token or not refresh_token:
        raise click.ClickException(
            "Provisioning bundle is missing access_token/refresh_token."
        )
    expires_at = bundle.get("access_expires_at")
    expires_in = max(int(expires_at - time.time()), 60) if expires_at else 300

    manager = TokenManager(web_backend_url=web_backend_url)
    manager.store_tokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )


def _write_owner_identity(owner: dict) -> None:
    sub = str(owner.get("sub") or "")
    if not sub:
        raise click.ClickException("Bootstrap response did not include an owner.")
    payload = {"sub": sub}
    if owner.get("email"):
        payload["email"] = str(owner["email"]).strip().lower()

    OWNER_IDENTITY_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OWNER_IDENTITY_JSON_PATH.with_suffix(f".json.tmp{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
        os.replace(temporary, OWNER_IDENTITY_JSON_PATH)
    finally:
        if temporary.exists():
            temporary.unlink()


def _stage_single_use_netmesh_key(auth_key: str) -> None:
    if not auth_key:
        raise click.ClickException(
            "Bootstrap response did not include a Netmesh enrollment key."
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(NETMESH_AUTHKEY_FILE, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(auth_key)


def _exchange_bootstrap(bootstrap_token: str, web_backend_url: str) -> dict:
    if urlsplit(web_backend_url).scheme != "https":
        raise click.ClickException("Workspace bootstrap requires an HTTPS backend.")
    try:
        response = httpx.post(
            f"{web_backend_url.rstrip('/')}{BOOTSTRAP_EXCHANGE_PATH}",
            headers={"Authorization": f"Openbase-Bootstrap {bootstrap_token}"},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"Workspace bootstrap request failed: {exc}"
        ) from exc
    if response.status_code >= 400:
        raise click.ClickException(
            f"Workspace bootstrap was rejected with HTTP {response.status_code}."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise click.ClickException(
            "Workspace bootstrap returned an invalid response."
        ) from exc
    if not isinstance(payload, dict):
        raise click.ClickException("Workspace bootstrap returned an invalid response.")
    scopes = payload.get("machine_token_scopes")
    if scopes != ["llm_proxy", "audio_proxy"]:
        raise click.ClickException("Bootstrap returned invalid machine token scopes.")
    owner = payload.get("owner")
    netmesh = payload.get("netmesh")
    if (
        not isinstance(owner, dict)
        or not isinstance(netmesh, dict)
        or not owner.get("sub")
        or not netmesh.get("control_url")
        or not netmesh.get("auth_key")
    ):
        raise click.ClickException("Bootstrap response was incomplete.")
    try:
        MachineTokenManager(web_backend_url).store_bootstrap_token(
            token=str(payload.get("machine_token") or ""),
            token_prefix=str(payload.get("machine_token_prefix") or ""),
            install_id=str(payload.get("machine_token_install_id") or ""),
            scopes=scopes,
        )
    except MachineTokenError as exc:
        raise click.ClickException(str(exc)) from exc
    _write_owner_identity(owner)
    _stage_single_use_netmesh_key(str(netmesh.get("auth_key") or ""))
    return netmesh


def _join_tailnet(authkey: str, hostname: str) -> None:
    if not authkey:
        return
    # --ssh: workspaces must accept Tailscale SSH from the owner's devices.
    # --operator: the session user manages Tailscale Serve without sudo, which
    # the later setup step requires.
    command = [
        "sudo",
        "tailscale",
        "up",
        "--authkey",
        authkey,
        "--ssh",
        f"--operator={getpass.getuser()}",
    ]
    if hostname:
        command += ["--hostname", hostname]
    subprocess.run(command, check=True)


def _disable_desktop() -> None:
    """Turn off the GUI on headless workspaces so a shared AMI stays cheap."""
    subprocess.run(
        ["sudo", "systemctl", "set-default", "multi-user.target"], check=False
    )
    for unit in ("gdm3", "dcvserver"):
        subprocess.run(["sudo", "systemctl", "disable", "--now", unit], check=False)


@click.command()
@click.option(
    "--input-file",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    default=None,
    help="Path to a JSON provisioning bundle (from openbase-cloud user-data).",
)
@click.option(
    "--kind",
    type=click.Choice(["full", "headless", "container"]),
    default=None,
    help="Workspace kind. Headless disables the desktop.",
)
@click.option("--access-token", default=None, help="Override bundle access token.")
@click.option("--refresh-token", default=None, help="Override bundle refresh token.")
@click.option(
    "--tailscale-authkey", default=None, help="Override bundle Tailscale auth key."
)
@click.option(
    "--tailscale-hostname", default=None, help="Override bundle Tailscale hostname."
)
@click.pass_context
def provision(
    ctx: click.Context,
    input_file: str | None,
    kind: str | None,
    access_token: str | None,
    refresh_token: str | None,
    tailscale_authkey: str | None,
    tailscale_hostname: str | None,
) -> None:
    """Provision this workspace from an injected credential bundle."""
    if platform.system() != "Linux":
        raise click.ClickException("provision is only supported on Linux workspaces.")

    bundle = _load_bundle(
        input_file,
        {
            "kind": kind,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "tailscale_authkey": tailscale_authkey,
            "tailscale_hostname": tailscale_hostname,
        },
    )

    kind = bundle.get("kind", "full")
    web_backend_url = bundle.get("web_backend_url") or DEFAULT_WEB_BACKEND_URL

    # 1. Point the CLI at Openbase Cloud and establish the installation
    # identity. Container workspaces exchange only a one-time grant; they never
    # receive or store the owner's access/refresh token pair.
    DEFAULT_ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    post_setup_env: dict[str, str] = {}
    if kind == "container":
        bootstrap_token = os.environ.pop(BOOTSTRAP_TOKEN_ENV_KEY, "")
        machine_tokens = MachineTokenManager(web_backend_url)
        staged_exchange_is_complete = (
            machine_tokens.has_cached_token()
            and OWNER_IDENTITY_JSON_PATH.is_file()
            and NETMESH_AUTHKEY_FILE.is_file()
        )
        if staged_exchange_is_complete:
            netmesh = {
                "control_url": env_file_values(DEFAULT_ENV_FILE_PATH).get(
                    "OPENBASE_TSNET_CONTROL_URL", "https://net.openbase.cloud"
                )
            }
        else:
            if not bootstrap_token:
                raise click.ClickException(
                    f"Container provisioning requires {BOOTSTRAP_TOKEN_ENV_KEY}."
                )
            netmesh = _exchange_bootstrap(bootstrap_token, web_backend_url)
        post_setup_env = {
            WEB_BACKEND_ENV_KEY: web_backend_url,
            "OPENBASE_CODER_CLI_TAILSCALE_PROVIDER": "netmesh-tsnet",
            "OPENBASE_TSNET_CONTROL_URL": str(netmesh["control_url"]),
            "OPENBASE_TSNET_STATE_DIR": os.environ.get(
                "OPENBASE_TSNET_STATE_DIR",
                str(DEFAULT_ENV_FILE_PATH.parent / "tsnet"),
            ),
            "LIVEKIT_CODEX_THREAD_CWD": os.environ.get(
                "OPENBASE_CODER_PROJECTS_DIR", "/data/workspace"
            ),
        }
    else:
        upsert_env_file_values(
            DEFAULT_ENV_FILE_PATH, {WEB_BACKEND_ENV_KEY: web_backend_url}
        )
        _store_auth(bundle, web_backend_url)

    # 2. Join the tailnet so the box is reachable / heartbeats can be sent.
    if kind != "container":
        _join_tailnet(
            bundle.get("tailscale_authkey", ""),
            bundle.get("tailscale_hostname", ""),
        )

    # 3. Headless workspaces have no desktop.
    if kind == "headless":
        _disable_desktop()

    # 4. Normal setup: install/start services, configure Tailscale Serve, and
    #    register the device with Openbase Cloud.
    ctx.invoke(
        setup,
        env_file=str(DEFAULT_ENV_FILE_PATH),
        coding_backend="openbase_cloud",
        audio_provider="openbase-cloud",
        tailnet_provider="netmesh-tsnet" if kind == "container" else None,
        skip_services=kind == "container",
        json_progress=False,
    )
    if post_setup_env:
        # Setup must create the complete env file on a new volume before we
        # layer the container-only values onto it. Pre-creating a partial file
        # would skip generation of local runtime credentials.
        upsert_env_file_values(DEFAULT_ENV_FILE_PATH, post_setup_env)

    # 5. Install cloud-only boot services.
    if kind != "container":
        _install_cloud_workspace_services()

    # 6. Optional code sync (bundles may omit the field entirely).
    if bundle.get("code_sync") is True:
        _enable_code_sync()

    click.echo(f"Provisioned {kind} workspace against {web_backend_url}.")


def _install_cloud_workspace_services() -> None:
    from openbase_coder_cli.services.launchd import install_service
    from openbase_coder_cli.services.registry import find_service, require_installation

    config = require_installation()
    install_service(config, find_service("openbase-cloud-auth-rehydrate"))
    install_service(config, find_service("openbase-cloud-heartbeat"))


SYNCTHING_RELEASES_API = (
    "https://api.github.com/repos/syncthing/syncthing/releases/latest"
)
_SYNCTHING_ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64"}


def _ensure_syncthing_linux() -> None:
    """Install syncthing on a Linux workspace via the managed installer."""
    from openbase_coder_cli.code_sync.install import ensure_syncthing_installed

    ensure_syncthing_installed()


def _download_syncthing_release() -> None:
    """Download the latest static syncthing binary into ~/.openbase/bin."""
    import tarfile
    import tempfile

    import httpx

    from openbase_coder_cli.paths import OPENBASE_BIN_DIR

    machine = platform.machine().lower()
    arch = _SYNCTHING_ARCHES.get(machine)
    if arch is None:
        raise click.ClickException(f"Unsupported syncthing architecture: {machine}")

    release = httpx.get(SYNCTHING_RELEASES_API, timeout=30).json()
    version = str(release.get("tag_name", "")).strip()
    if not version:
        raise click.ClickException("Could not determine the latest syncthing release.")
    archive_name = f"syncthing-linux-{arch}-{version}.tar.gz"
    url = (
        "https://github.com/syncthing/syncthing/releases/download/"
        f"{version}/{archive_name}"
    )

    OPENBASE_BIN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp_dir:
        archive_path = Path(tmp_dir) / archive_name
        with httpx.stream("GET", url, timeout=120, follow_redirects=True) as response:
            response.raise_for_status()
            with archive_path.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        with tarfile.open(archive_path) as archive:
            member = next(
                (
                    item
                    for item in archive.getmembers()
                    if item.isfile() and Path(item.name).name == "syncthing"
                ),
                None,
            )
            if member is None:
                raise click.ClickException(
                    f"No syncthing binary found in {archive_name}."
                )
            member.name = "syncthing"
            archive.extract(member, OPENBASE_BIN_DIR)
    (OPENBASE_BIN_DIR / "syncthing").chmod(0o755)


def _enable_code_sync() -> None:
    """Best-effort code-sync arming for provisioned workspaces.

    Forced because the user's other devices may register their sync
    capabilities after this workspace boots; the rendered config is refreshed
    on every settings change and reconcile tick.
    """
    from openbase_coder_cli.code_sync import CodeSyncError
    from openbase_coder_cli.code_sync.manager import enable_code_sync

    try:
        _ensure_syncthing_linux()
        enable_code_sync(force=True)
    except (click.ClickException, CodeSyncError) as exc:
        click.echo(click.style(f"  WARN  code sync not enabled: {exc}", fg="yellow"))
    else:
        click.echo("Enabled code sync.")
