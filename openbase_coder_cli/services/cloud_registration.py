"""Register this device and report CLI onboarding state to openbase-cloud.

Implements the CLI side of the device-registration protocol in the workspace
``specs/onboarding/cloud-api.md`` spec. The backend endpoints may not exist
yet; every call degrades to a non-fatal result (``supported=False`` on 404)
so login/setup never fail because of them. The last results are cached in
``~/.openbase/onboarding.json`` for the local onboarding status payload.
"""

from __future__ import annotations

import platform
import socket
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from openbase_coder_cli._version import __version__
from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
    TokenManager,
)
from openbase_coder_cli.services.onboarding import (
    compute_cli_configured,
    read_onboarding_cache,
    web_backend_url,
    write_onboarding_cache,
)
from openbase_coder_cli.services.tailnet_devices import tailscale_self_identity
from openbase_coder_cli.services.tailscale_serve import tailscale_serve_health

DEVICE_REGISTER_PATH = "/api/openbase/devices/register/"
NETMESH_ENROLL_PATH = "/api/openbase/netmesh/enroll/"
NETMESH_DEVICES_PATH = "/api/openbase/netmesh/devices/"
TAILNET_PROVIDER_PATH = "/api/openbase/tailnet-provider/"
REQUEST_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class CloudReportResult:
    ok: bool
    supported: bool
    error: str | None = None
    status_code: int | None = None
    response: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def device_registration_payload() -> dict[str, Any]:
    identity = tailscale_self_identity()
    payload: dict[str, Any] = {
        "device_id": _device_id(),
        "kind": "desktop",
        "hostname": socket.gethostname(),
        "display_name": socket.gethostname(),
        "platform": platform.system().lower(),
        "os_version": (
            platform.mac_ver()[0]
            if platform.system() == "Darwin"
            else platform.release()
        ),
        "version": __version__,
    }
    if identity["available"]:
        payload.update(
            {
                "tailscale": {
                    "dns_name": identity["dns_name"],
                    "node_hostname": identity["node_hostname"],
                    "tailnet": identity["tailnet"],
                    "ips": identity["ips"],
                },
                "tailscale_magic_dns": identity["dns_name"] or "",
                "capabilities": {
                    "tailscale_ips": identity["ips"],
                },
            }
        )
        if identity["ips"]:
            payload["tailscale_ip"] = identity["ips"][0]
    return payload


def _device_id() -> str:
    cache = read_onboarding_cache()
    existing = cache.get("device_id")
    if isinstance(existing, str) and existing:
        return existing
    generated = f"desktop-{uuid.uuid4().hex}"
    write_onboarding_cache({"device_id": generated})
    return generated


def local_device_id() -> str:
    """This device's stable cloud registration ID."""
    return _device_id()


def _code_sync_capabilities() -> dict[str, Any]:
    """Advertised code-sync facts; never raises (registration must not fail)."""
    from openbase_coder_cli.code_sync.syncthing import stored_device_id
    from openbase_coder_cli.sync_config import code_sync_enabled

    capabilities: dict[str, Any] = {}
    try:
        capabilities["code_sync_enabled"] = code_sync_enabled()
    except ValueError:
        capabilities["code_sync_enabled"] = False
    syncthing_device_id = stored_device_id()
    if syncthing_device_id:
        capabilities["syncthing_device_id"] = syncthing_device_id
    return capabilities


def _is_standalone_install() -> bool:
    """Install mode for the report; never raises (registration must not fail)."""
    from openbase_coder_cli.runtime import is_standalone_runtime

    try:
        return is_standalone_runtime()
    except OSError:
        return False


def _with_capabilities(
    payload: dict[str, Any], capabilities: dict[str, Any]
) -> dict[str, Any]:
    return {
        **payload,
        "capabilities": {
            **payload.get("capabilities", {}),
            **capabilities,
        },
    }


def register_device_with_cloud() -> CloudReportResult:
    """POST this device's identity to openbase-cloud. Never raises."""
    result = _post_to_cloud(DEVICE_REGISTER_PATH, device_registration_payload())
    write_onboarding_cache({"last_register": {"at": _timestamp(), **result.to_dict()}})
    return result


def report_cli_state(
    *,
    cli_configured: bool | None = None,
    serve_healthy: bool | None = None,
) -> CloudReportResult:
    """Advertise this device's current CLI facts to openbase-cloud. Never raises."""
    if cli_configured is None:
        cli_configured = compute_cli_configured()
    if serve_healthy is None:
        serve_healthy = tailscale_serve_health().healthy
    payload = _with_capabilities(
        device_registration_payload(),
        {
            "cli_configured": cli_configured,
            "cli_version": __version__,
            # Dev installs report scm-derived versions (e.g. 0.3.dev12+g...)
            # that must never be compared against release version policy
            # (minimum_cli_version); the mode lets the cloud exempt them.
            "cli_install_mode": (
                "standalone" if _is_standalone_install() else "dev-workspace"
            ),
            "tailscale_serve_healthy": serve_healthy,
            **_code_sync_capabilities(),
        },
    )
    result = _post_to_cloud(
        DEVICE_REGISTER_PATH,
        payload,
    )
    cache_update: dict[str, Any] = {
        "last_report": {
            "at": _timestamp(),
            "cli_configured": cli_configured,
            "serve_healthy": serve_healthy,
            **result.to_dict(),
        }
    }
    if result.response and result.response.get("minimum_cli_version"):
        cache_update["cloud_policy"] = {
            "minimum_cli_version": str(result.response["minimum_cli_version"]),
            "at": _timestamp(),
        }
    write_onboarding_cache(cache_update)
    return result


def register_and_report(
    *,
    cli_configured: bool | None = None,
    serve_healthy: bool | None = None,
) -> CloudReportResult:
    """Register the device and advertise CLI facts. Never raises.

    Returns the first failing result so callers can surface a single warning.
    """
    return report_cli_state(cli_configured=cli_configured, serve_healthy=serve_healthy)


def netmesh_enroll() -> dict[str, Any] | None:
    """Mint a netmesh pre-auth key for the signed-in user. Never raises.

    Returns the enroll payload ({control_url, auth_key, ...}) or None when the
    user is not logged in, the cloud errors, or netmesh is not deployed there.
    """
    result = _post_to_cloud(NETMESH_ENROLL_PATH, {})
    if not result.ok or not isinstance(result.response, dict):
        return None
    if not result.response.get("auth_key") or not result.response.get("control_url"):
        return None
    return result.response


def list_netmesh_devices() -> list[dict[str, Any]]:
    """This user's netmesh (headscale) nodes. Never raises; [] on any failure."""
    result = _post_to_cloud(NETMESH_DEVICES_PATH, {}, method="GET")
    if not result.ok or not isinstance(result.response, list):
        return []
    return [entry for entry in result.response if isinstance(entry, dict)]


def revoke_netmesh_device(node_id: str) -> bool:
    """Delete one of this user's netmesh nodes. Never raises."""
    result = _post_to_cloud(f"{NETMESH_DEVICES_PATH}{node_id}/", {}, method="DELETE")
    return result.ok


def push_tailnet_provider(provider: str) -> CloudReportResult:
    """Record the account-level tailnet transport in openbase-cloud.

    The three providers are three different networks, so the whole device
    fleet must converge on this value (other devices read it from onboarding
    state). Never raises; ``supported=False`` when the cloud predates the
    endpoint.
    """
    return _post_to_cloud(TAILNET_PROVIDER_PATH, {"provider": provider})


def fetch_tailnet_provider() -> str | None:
    """The account-level tailnet transport, or None when unavailable."""
    result = _post_to_cloud(TAILNET_PROVIDER_PATH, {}, method="GET")
    if not result.ok or not isinstance(result.response, dict):
        return None
    provider = result.response.get("provider")
    return provider if isinstance(provider, str) and provider else None


def _post_to_cloud(
    path: str, payload: dict[str, Any], *, method: str = "POST"
) -> CloudReportResult:
    backend_url = web_backend_url()
    try:
        token = TokenManager(backend_url).get_access_token()
    except AuthLoginRequiredError:
        return CloudReportResult(
            ok=False,
            supported=True,
            error="Login required. Run 'openbase-coder login' first.",
        )
    except AuthTransientError as exc:
        return CloudReportResult(ok=False, supported=True, error=str(exc))

    try:
        response = httpx.request(
            method,
            f"{backend_url}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return CloudReportResult(ok=False, supported=True, error=str(exc))

    if response.status_code >= 400 and _endpoint_unsupported(response):
        return CloudReportResult(
            ok=False,
            supported=False,
            status_code=response.status_code,
            error="openbase-cloud does not support device registration yet.",
        )
    if response.status_code >= 400:
        return CloudReportResult(
            ok=False,
            supported=True,
            status_code=response.status_code,
            error=f"HTTP {response.status_code}: {response.text[:200]}",
        )
    try:
        response_payload = response.json()
    except ValueError:
        response_payload = None
    return CloudReportResult(
        ok=True,
        supported=True,
        status_code=response.status_code,
        response=response_payload if isinstance(response_payload, dict) else None,
    )


def _endpoint_unsupported(response: httpx.Response) -> bool:
    """Whether an error response means the endpoint has not shipped yet.

    The real endpoints are DRF views that return JSON errors; a 404/405 or an
    HTML error page (e.g. Django's CSRF failure page) means the backend does
    not implement the onboarding contract yet.
    """
    if response.status_code in (404, 405):
        return True
    content_type = response.headers.get("content-type", "")
    return content_type.startswith("text/html")


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
