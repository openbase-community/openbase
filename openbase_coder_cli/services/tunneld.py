"""Client for openbase-tunneld, the embedded-tsnet daemon (tunneld/ in this repo).

When the tailnet provider is ``netmesh-tsnet`` (see ``tailscale_provider``),
the CLI talks to the daemon's loopback control API instead of shelling out to
a tailscale binary or the netmesh-ctl shim. The daemon serves ``/status`` in
the same JSON schema as ``tailscale status --json``, so existing parsers work
on its payload unchanged.

Adapted from the tsnet prototype branch (openbase#10): the provider env var
replaces the prototype's ``OPENBASE_TSNET`` flag + cloud rollout policy, and
cloud auth-key minting goes through the netmesh enroll API rather than the
Tailscale-SaaS key endpoint (which was never deployed).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from openbase_coder_cli.paths import OPENBASE_BIN_DIR
from openbase_coder_cli.services.installation import InstallationConfig

TUNNELD_LOCAL_API = os.environ.get("OPENBASE_TUNNELD_URL", "http://127.0.0.1:7998")
TUNNELD_TIMEOUT_SECONDS = 5
TUNNELD_PROBE_TIMEOUT_SECONDS = 8
TUNNELD_START_WAIT_SECONDS = 15


def _go_binary() -> str | None:
    return shutil.which("go") or (
        "/opt/homebrew/bin/go" if os.access("/opt/homebrew/bin/go", os.X_OK) else None
    )


def install_tunneld_binary(config: InstallationConfig) -> Path:
    """Install the tunneld executable into Openbase's stable user bin dir.

    Development installs build the checked-out Go source so service wrappers
    never depend on an ignored workspace build artifact. Packaged installs
    copy the bundled executable into the same stable location.
    """
    target = OPENBASE_BIN_DIR / "openbase-tunneld"
    workspace = (
        Path(config.workspace_path).expanduser() if config.workspace_path else None
    )
    source_dir = workspace / "cli" / "tunneld" if workspace else None

    source_binary: Path | None = None
    build_command: list[str] | None = None
    if source_dir and (source_dir / "go.mod").is_file():
        go = _go_binary()
        if not go:
            raise RuntimeError(
                "the Go toolchain is required to build openbase-tunneld; "
                "install Go and re-run setup"
            )
        build_command = [go, "build"]
    else:
        packaged = _packaged_binary()
        if packaged:
            source_binary = Path(packaged)
        elif target.is_file() and os.access(target, os.X_OK):
            return target
        else:
            raise RuntimeError(
                "openbase-tunneld source or a packaged executable was not found"
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=".openbase-tunneld-", dir=target.parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        if build_command is not None:
            temporary.unlink()
            result = subprocess.run(  # noqa: S603 - resolved Go executable, fixed args
                [*build_command, "-o", str(temporary), "."],
                cwd=source_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                suffix = f": {detail}" if detail else ""
                raise RuntimeError(f"openbase-tunneld build failed{suffix}")
        else:
            assert source_binary is not None
            shutil.copy2(source_binary, temporary)
        temporary.chmod(0o755)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _state_dir() -> Path:
    configured = os.environ.get("OPENBASE_TSNET_STATE_DIR")
    if configured:
        return Path(configured)
    return Path.home() / ".openbase" / "tsnet"


def _control_headers() -> dict[str, str]:
    """Bearer token minted by the daemon into <statedir>/control.token."""
    token = os.environ.get("OPENBASE_TUNNELD_TOKEN")
    if not token:
        try:
            token = (_state_dir() / "control.token").read_text().strip()
        except OSError:
            return {}
    return {"Authorization": f"Bearer {token}"}


def _packaged_binary() -> str | None:
    """The bundled daemon inside a standalone package, when running from one."""
    package_dir = os.environ.get("OPENBASE_CODER_PACKAGE_DIR")
    if not package_dir:
        return None
    candidate = Path(package_dir) / "bin" / "openbase-tunneld"
    return str(candidate) if os.access(candidate, os.X_OK) else None


def tunneld_binary() -> str | None:
    """Locate the openbase-tunneld binary (env override, bin dir, package, PATH)."""
    override = os.environ.get("OPENBASE_TUNNELD_BIN")
    if override and os.access(override, os.X_OK):
        return override
    bin_dir_candidate = Path.home() / ".openbase" / "bin" / "openbase-tunneld"
    if os.access(bin_dir_candidate, os.X_OK):
        return str(bin_dir_candidate)
    return _packaged_binary() or shutil.which("openbase-tunneld")


def tunneld_tool_path() -> str | None:
    """Control-tool marker for the provider layer.

    The daemon is the control surface, so "installed" means either the binary
    is locatable or a daemon has already run (its control token exists).
    """
    binary = tunneld_binary()
    if binary:
        return binary
    token_path = _state_dir() / "control.token"
    return str(token_path) if token_path.is_file() else None


def voice_turn_info() -> dict[str, Any] | None:
    """TURN relay credentials for embedded-mode WebRTC media.

    The daemon mints them into ``<statedir>/turn.json`` and runs the relay on
    the tailnet; the phone forces its LiveKit media through it because an
    in-app tsnet node has no OS route for WebRTC's UDP sockets. Served only
    over loopback and the user's own tailnet.
    """
    import json

    try:
        raw = json.loads((_state_dir() / "turn.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not raw.get("username") or not raw.get("password"):
        return None
    return {
        "username": raw["username"],
        "password": raw["password"],
        "port": raw.get("port", 3478),
        "realm": raw.get("realm", "openbase"),
    }


def tunneld_status() -> tuple[bool, dict[str, Any] | None, str | None]:
    """Fetch node status from tunneld.

    Returns ``(tunneld_available, status_payload, error)``; ``status_payload``
    matches the ``tailscale status --json`` schema.
    """
    try:
        response = httpx.get(
            f"{TUNNELD_LOCAL_API}/status",
            headers=_control_headers(),
            timeout=TUNNELD_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return (
            False,
            None,
            f"openbase-tunneld is not reachable at {TUNNELD_LOCAL_API}: {exc}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        return True, None, f"Unable to parse tunneld status JSON: {exc}"

    if response.status_code != 200:
        return (
            True,
            None,
            str(payload.get("error") or f"HTTP {response.status_code} from tunneld"),
        )
    return True, payload, None


def tunneld_health() -> dict[str, Any]:
    try:
        response = httpx.get(
            f"{TUNNELD_LOCAL_API}/health",
            headers=_control_headers(),
            timeout=TUNNELD_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return {"reachable": False, "error": str(exc)}
    if response.status_code == 401:
        # The daemon is up but our token is stale/missing; do not treat this
        # as unreachable or a caller may spawn a duplicate daemon.
        return {
            "reachable": True,
            "error": "control token rejected (check <statedir>/control.token)",
        }
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"reachable": False, "error": str(exc)}
    payload["reachable"] = True
    return payload


def tunneld_probe(
    host: str, port: int = 18080, path: str = "/api/health/"
) -> dict[str, Any]:
    """Dial a tailnet peer through the embedded node (host network can't)."""
    try:
        response = httpx.get(
            f"{TUNNELD_LOCAL_API}/probe",
            params={"host": host, "port": str(port), "path": path},
            headers=_control_headers(),
            timeout=TUNNELD_PROBE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "error": f"tunneld probe failed: {exc}"}


def tunneld_login(auth_key: str) -> bool:
    """Log the running daemon into the tailnet with an auth key."""
    try:
        response = httpx.post(
            f"{TUNNELD_LOCAL_API}/login",
            json={"auth_key": auth_key},
            headers=_control_headers(),
            timeout=TUNNELD_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _managed_service_installed() -> bool:
    """Whether the platform service manager owns the tunneld process."""
    from openbase_coder_cli.services.definitions import TUNNELD_SERVICE
    from openbase_coder_cli.services.launchd import launchctl_status

    return bool(launchctl_status(TUNNELD_SERVICE).get("installed"))


def ensure_tunneld_running(
    auth_key: str | None = None,
    *,
    managed_service: bool | None = None,
) -> None:
    """Start openbase-tunneld if needed and wait until it forwards traffic.

    ``auth_key`` (a netmesh headscale pre-auth key when available) is
    submitted to the daemon when it needs a tailnet login; without one, the
    daemon's interactive login URL is surfaced in the raised error.
    """
    health = tunneld_health()
    if managed_service is None:
        managed_service = _managed_service_installed()
    if not health.get("reachable"):
        if not managed_service:
            binary = tunneld_binary()
            if not binary:
                raise RuntimeError(
                    "openbase-tunneld is not running and no binary was found "
                    "(set OPENBASE_TUNNELD_BIN or add openbase-tunneld to PATH)."
                )
            from openbase_coder_cli.services.tailnet_hostname import (
                TSNET_HOSTNAME_ENV_KEY,
                netmesh_hostname,
            )

            # Manual/standalone callers have no service supervisor to start
            # tunneld. Managed installs must wait for their single owner
            # instead of racing it for the control port.
            env = dict(os.environ)
            env.setdefault(TSNET_HOSTNAME_ENV_KEY, netmesh_hostname())
            subprocess.Popen(
                [binary, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )

    login_submitted = False
    enroll_attempted = False
    deadline = time.monotonic() + TUNNELD_START_WAIT_SECONDS
    while True:
        health = tunneld_health()
        if health.get("backend_state") == "Running" and health.get("forwards_up"):
            return
        if health.get("backend_state") == "NeedsLogin":
            if not auth_key and not enroll_attempted:
                # Zero-touch: mint a netmesh key with the user's cloud login.
                # Imported lazily (cloud_registration -> tailscale_serve -> us).
                from openbase_coder_cli.services.cloud_registration import (
                    netmesh_enroll,
                )

                enroll_attempted = True
                enrollment = netmesh_enroll()
                if enrollment:
                    auth_key = enrollment["auth_key"]
            if auth_key and not login_submitted:
                login_submitted = tunneld_login(auth_key)
                if login_submitted:
                    # Key redemption takes a few seconds beyond normal startup.
                    deadline = max(
                        deadline,
                        time.monotonic() + TUNNELD_START_WAIT_SECONDS,
                    )
            elif not auth_key and health.get("auth_url"):
                raise RuntimeError(
                    "openbase-tunneld needs a tailnet login: open "
                    f"{health['auth_url']} or restart it with an auth key (TS_AUTHKEY)."
                )
        if time.monotonic() >= deadline:
            owner = "managed service" if managed_service else "standalone process"
            raise RuntimeError(
                f"openbase-tunneld {owner} did not reach Running state "
                f"(state: {health.get('backend_state') or health.get('error')})."
            )
        time.sleep(0.5)
