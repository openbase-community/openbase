from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

import httpx

OPENBASE_CODER_TAILNET_PORT = 18080
OPENBASE_CODER_LOCAL_PORT = 7999
LIVEKIT_TAILNET_PORT = 7880
LIVEKIT_LOCAL_PORT = 7880
OPENBASE_HEALTH_PATH = "/api/health/"
TAILSCALE_TIMEOUT_SECONDS = 5
TAILSCALE_HEALTH_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class TailscaleServeHealth:
    tailscale_available: bool
    tailscale_running: bool
    host: str | None
    openbase_url: str | None
    openbase_configured: bool
    livekit_configured: bool
    openbase_reachable: bool
    error: str | None = None

    @property
    def healthy(self) -> bool:
        return (
            self.tailscale_available
            and self.tailscale_running
            and self.openbase_configured
            and self.livekit_configured
            and self.openbase_reachable
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tailscale_available": self.tailscale_available,
            "tailscale_running": self.tailscale_running,
            "host": self.host,
            "openbase_url": self.openbase_url,
            "openbase_configured": self.openbase_configured,
            "livekit_configured": self.livekit_configured,
            "openbase_reachable": self.openbase_reachable,
            "healthy": self.healthy,
            "error": self.error,
        }


def openbase_serve_rules() -> list[dict[str, Any]]:
    """Canonical built-in Serve rules, kept separate from user publications."""
    return [
        {"kind": "openbase-console"},
        {"kind": "openbase-livekit"},
    ]


def configure_tailscale_serve() -> None:
    from openbase_coder_cli.services import tailscale_provider as tp
    from openbase_coder_cli.services.published_service_routes import (
        expected_serve_base_hash,
    )
    from openbase_coder_cli.services.published_services import (
        ServiceRegistry,
        load_registry,
        published_serve_rules,
        save_registry,
    )

    rules = [*openbase_serve_rules(), *published_serve_rules(persistent_only=True)]
    if (
        tp.is_netmesh()
        and not tp.is_netmesh_tsnet()
        and not tp.netmesh_uses_stock_tailscale()
    ):
        capability = tp.serve_capability()
        if not capability.get("supported"):
            tp.apply_serve_legacy(rules)
            return
        registry = load_registry()
        snapshot = tp.serve_snapshot()
        previous_rules = [*openbase_serve_rules(), *published_serve_rules()]
        expected_hash = expected_serve_base_hash(
            str(snapshot.get("hash")),
            previous_rules,
            registry.last_applied_serve_hash,
        )
        if snapshot.get("hash") != expected_hash:
            raise RuntimeError(
                "Openbase VPN Serve configuration drifted from the last known "
                "desired state; refusing to overwrite unknown routes."
            )
        result = tp.apply_serve(
            rules,
            expected_etag=str(snapshot["etag"]),
            expected_hash=expected_hash,
        )
        save_registry(
            ServiceRegistry(
                registry.services,
                str(result["hash"]),
            )
        )
        return
    tp.apply_serve(rules)


def reset_tailscale_serve() -> None:
    """Force the Openbase VPN Serve config back to the canonical Openbase rules.

    ``configure_tailscale_serve`` refuses when the live Serve config drifts from
    the last-applied hash — the right default, so a routine re-apply never
    clobbers routes we did not place. But the hardened tailscaled resets its own
    Serve config on some restarts, which leaves the live hash permanently
    mismatched with the recorded one and NO way back: the Openbase VPN has no
    equivalent of ``tailscale serve reset`` (and we deliberately do not shell out
    to the stock Tailscale client — the netmesh runs on our own headscale +
    signed helper, using Tailscale only for DERP relays). This re-applies the
    canonical rule set using the LIVE snapshot as the compare-and-swap base, so
    it always succeeds regardless of drift while staying safe against a
    concurrent writer, entirely through the signed netmesh helper.
    """
    from openbase_coder_cli.services import tailscale_provider as tp
    from openbase_coder_cli.services.published_services import (
        ServiceRegistry,
        load_registry,
        published_serve_rules,
        save_registry,
    )

    rules = [*openbase_serve_rules(), *published_serve_rules(persistent_only=True)]
    if not (
        tp.is_netmesh()
        and not tp.is_netmesh_tsnet()
        and not tp.netmesh_uses_stock_tailscale()
    ):
        # tsnet forwards natively; stock/other providers have their own reset.
        configure_tailscale_serve()
        return
    if not tp.serve_capability().get("supported"):
        tp.apply_serve_legacy(rules)
        return
    snapshot = tp.serve_snapshot()
    result = tp.apply_serve(
        rules,
        expected_etag=str(snapshot["etag"]),
        expected_hash=str(snapshot["hash"]),
    )
    registry = load_registry()
    save_registry(ServiceRegistry(registry.services, str(result["hash"])))


def tailscale_serve_health() -> TailscaleServeHealth:
    from openbase_coder_cli.services import tailscale_provider as tp

    if tp.is_netmesh_tsnet():
        return _tunneld_serve_health()

    if tp.tool_path() is None:
        return TailscaleServeHealth(
            tailscale_available=False,
            tailscale_running=False,
            host=None,
            openbase_url=None,
            openbase_configured=False,
            livekit_configured=False,
            openbase_reachable=False,
            error=f"{tp.provider()} control tool was not found.",
        )

    status = tp.status_json()
    if status.get("error"):
        return TailscaleServeHealth(
            tailscale_available=True,
            tailscale_running=False,
            host=None,
            openbase_url=None,
            openbase_configured=False,
            livekit_configured=False,
            openbase_reachable=False,
            error=str(status["error"]),
        )

    host = _self_dns_name(status)
    serve_status = tp.serve_status_json()
    if serve_status.get("error"):
        return TailscaleServeHealth(
            tailscale_available=True,
            tailscale_running=True,
            host=host,
            openbase_url=_openbase_url(host),
            openbase_configured=False,
            livekit_configured=False,
            openbase_reachable=False,
            error=str(serve_status["error"]),
        )

    openbase_configured = _openbase_serve_configured(serve_status, host)
    livekit_configured = _livekit_serve_configured(serve_status)
    openbase_url = _openbase_url(host)
    # Probe reachability via the tailnet IP rather than the MagicDNS name: the
    # backend can reach the IP with no DNS, so this doesn't depend on system
    # MagicDNS being configured (which differs between Tailscale and netmesh, and
    # isn't wired up on macOS for netmesh). But `tailscale serve --http` mounts are
    # name-based (virtual-hosted), so an IP request 404s — we present the MagicDNS
    # name in the Host header so the serve mount and Django ALLOWED_HOSTS match.
    probe_ip = _self_tailnet_ipv4(status)
    probe_url = _openbase_url(probe_ip) or openbase_url
    host_header = (
        f"{host}:{OPENBASE_CODER_TAILNET_PORT}" if (probe_ip and host) else None
    )
    openbase_reachable, reachability_error = _openbase_reachable(probe_url, host_header)

    return TailscaleServeHealth(
        tailscale_available=True,
        tailscale_running=True,
        host=host,
        openbase_url=openbase_url,
        openbase_configured=openbase_configured,
        livekit_configured=livekit_configured,
        openbase_reachable=openbase_reachable,
        error=reachability_error,
    )


def _tunneld_serve_health() -> TailscaleServeHealth:
    """Serve health for the embedded (netmesh-tsnet) transport.

    The tunneld daemon forwards the routes natively, so "configured" means its
    forwards are up, and reachability is verified by dialing our own node
    through the daemon (the host network stack has no route into the tailnet).
    Adapted from the tsnet prototype branch, keyed on the provider.
    """
    from openbase_coder_cli.services.tunneld import tunneld_health, tunneld_probe

    health = tunneld_health()
    if not health.get("reachable"):
        return TailscaleServeHealth(
            tailscale_available=False,
            tailscale_running=False,
            host=None,
            openbase_url=None,
            openbase_configured=False,
            livekit_configured=False,
            openbase_reachable=False,
            error=str(health.get("error") or "openbase-tunneld is not reachable."),
        )

    running = health.get("backend_state") == "Running"
    host = str(health.get("self_dns_name") or "").strip().rstrip(".") or None
    forwards_up = bool(health.get("forwards_up"))
    openbase_url = _openbase_url(host)

    error: str | None = None
    if not running:
        if health.get("auth_url"):
            error = f"tunneld needs a tailnet login: {health['auth_url']}"
        else:
            error = f"tunneld backend state is {health.get('backend_state')}."

    openbase_reachable = False
    if running and forwards_up and host:
        probe = tunneld_probe(host, OPENBASE_CODER_TAILNET_PORT, OPENBASE_HEALTH_PATH)
        if probe.get("ok"):
            openbase_reachable = True
        else:
            # Self-dial can lag right after startup; fall back to checking the
            # local backend directly so a healthy install isn't flagged.
            openbase_reachable, error = _openbase_reachable(
                f"http://127.0.0.1:{OPENBASE_CODER_LOCAL_PORT}"
            )

    return TailscaleServeHealth(
        tailscale_available=True,
        tailscale_running=running,
        host=host,
        openbase_url=openbase_url,
        openbase_configured=forwards_up,
        livekit_configured=forwards_up,
        openbase_reachable=openbase_reachable,
        error=error,
    )


TAILSCALE_APP_BUNDLE_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
# launchd services run with a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin),
# so PATH lookup alone silently fails in exactly the contexts that register
# the device — and a registration without a tailscale identity makes peers
# drop this device from their sync configs.
TAILSCALE_FALLBACK_PATHS = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    TAILSCALE_APP_BUNDLE_CLI,
)


def _tailscale_bin() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    # Direct-download and App Store installs don't put the CLI on PATH; the
    # binary inside the app bundle speaks the same CLI.
    for candidate in TAILSCALE_FALLBACK_PATHS:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _run_tailscale(tailscale_bin: str, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [tailscale_bin, *args],
        capture_output=True,
        text=True,
        timeout=TAILSCALE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "tailscale failed."
        raise RuntimeError(detail)
    return result


def _tailscale_status(tailscale_bin: str) -> dict[str, Any]:
    try:
        result = _run_tailscale(tailscale_bin, "status", "--json")
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        return {"error": f"Unable to run tailscale status: {exc}"}

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"Unable to parse tailscale status JSON: {exc}"}


def _tailscale_serve_status(tailscale_bin: str) -> dict[str, Any]:
    try:
        result = _run_tailscale(tailscale_bin, "serve", "status", "--json")
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        return {"error": f"Unable to run tailscale serve status: {exc}"}

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"Unable to parse tailscale serve status JSON: {exc}"}
    return payload if isinstance(payload, dict) else {}


def _self_dns_name(status: dict[str, Any]) -> str | None:
    self_payload = status.get("Self")
    if not isinstance(self_payload, dict):
        return None
    dns_name = self_payload.get("DNSName")
    if not isinstance(dns_name, str):
        return None
    return dns_name.strip().rstrip(".") or None


def _self_tailnet_ipv4(status: dict[str, Any]) -> str | None:
    """This node's tailnet IPv4, for a DNS-independent reachability probe."""
    self_payload = status.get("Self")
    if not isinstance(self_payload, dict):
        return None
    for ip in self_payload.get("TailscaleIPs") or []:
        if isinstance(ip, str) and "." in ip and ":" not in ip:
            return ip
    return None


def _openbase_url(host: str | None) -> str | None:
    if not host:
        return None
    return f"http://{_url_host_literal(host)}:{OPENBASE_CODER_TAILNET_PORT}"


def _openbase_serve_configured(payload: dict[str, Any], host: str | None) -> bool:
    tcp = payload.get("TCP")
    web = payload.get("Web")
    tcp_port = str(OPENBASE_CODER_TAILNET_PORT)
    if not isinstance(tcp, dict) or not isinstance(tcp.get(tcp_port), dict):
        return False
    if not tcp[tcp_port].get("HTTP"):
        return False
    if not host or not isinstance(web, dict):
        return True

    expected_host = f"{host}:{OPENBASE_CODER_TAILNET_PORT}"
    entry = web.get(expected_host)
    if not isinstance(entry, dict):
        return False
    handlers = entry.get("Handlers")
    if not isinstance(handlers, dict):
        return False
    root = handlers.get("/")
    return (
        isinstance(root, dict)
        and root.get("Proxy") == f"http://127.0.0.1:{OPENBASE_CODER_LOCAL_PORT}"
    )


def _livekit_serve_configured(payload: dict[str, Any]) -> bool:
    tcp = payload.get("TCP")
    if not isinstance(tcp, dict):
        return False
    entry = tcp.get(str(LIVEKIT_TAILNET_PORT))
    return (
        isinstance(entry, dict)
        and entry.get("TCPForward") == f"127.0.0.1:{LIVEKIT_LOCAL_PORT}"
    )


def _openbase_reachable(
    openbase_url: str | None, host_header: str | None = None
) -> tuple[bool, str | None]:
    if not openbase_url:
        return False, "Tailscale DNS name is unavailable."
    url = f"{openbase_url}{OPENBASE_HEALTH_PATH}"
    # When probing by IP, present the MagicDNS name via the Host header so the
    # name-based `tailscale serve --http` mount and Django ALLOWED_HOSTS accept it.
    headers = {"Host": host_header} if host_header else None
    try:
        response = httpx.get(
            url, timeout=TAILSCALE_HEALTH_TIMEOUT_SECONDS, headers=headers
        )
    except httpx.HTTPError as exc:
        return False, str(exc)

    if response.status_code != 200:
        return False, f"HTTP {response.status_code} from {url}"

    try:
        payload = response.json()
    except ValueError:
        return False, f"Invalid JSON response from {url}"

    if payload.get("status") != "ok":
        return False, f"Unexpected health response from {url}"
    return True, None


def _url_host_literal(host: str) -> str:
    if ":" in host and not host.startswith("[") and not host.endswith("]"):
        return f"[{host}]"
    return host
