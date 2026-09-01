"""Tailnet provider abstraction.

openbase-coder can ride on either the official **Tailscale** client or Openbase's
own **netmesh** (self-hosted headscale + a hardened macOS client). They are not
usable at the same time; the active one is selected by the
``OPENBASE_CODER_CLI_TAILSCALE_PROVIDER`` env var (``tailscale`` by default, or
``netmesh``). Set it at setup and switch it later via the CLI.

The two providers are reached differently, so this module abstracts the three
operations coder needs (status/serve-status/apply-serve) rather than swapping a
binary:

* ``tailscale``  → the ``tailscale`` CLI on the default socket (unchanged
  behaviour; fully backwards compatible).
* ``netmesh``    → the signed ``netmesh-ctl`` shim, which reaches the hardened
  root daemon over code-signature-verified XPC. coder never touches netmesh's
  root-only tailscaled socket.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

TIMEOUT_SECONDS = 5

PROVIDER_TAILSCALE = "tailscale"
PROVIDER_NETMESH = "netmesh"
# Same self-hosted headscale as ``netmesh``, but joined by an in-process embedded
# node (tsnet on the Mac via openbase-tunneld; TailscaleKit on iOS) instead of a
# device-wide VPN — so NO system extension / VPN profile is needed on either end
# and only the Openbase apps' own traffic rides the tailnet. The embedded
# transport (tunneld) is staged separately; selecting this records the choice.
PROVIDER_NETMESH_TSNET = "netmesh-tsnet"

PROVIDER_VALUES = (PROVIDER_TAILSCALE, PROVIDER_NETMESH, PROVIDER_NETMESH_TSNET)

# Last-resort netmesh-ctl location: the legacy standalone Openbase Netmesh app
# (retired). The shipping companion (nested in the desktop app) and the dev
# companion-build/DerivedData layouts are resolved from netmesh_companion —
# the single source of truth for where the companion lives — so they are not
# re-declared here.
NETMESH_CTL_CANDIDATES = (
    "/Applications/OpenbaseNetmesh.app/Contents/MacOS/netmesh-ctl",
)

_TAILSCALE_FALLBACK_PATHS = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


def provider() -> str:
    """The active provider name (defaults to ``tailscale``).

    ``~/.openbase/.env`` is the single source of truth: interactive CLI
    invocations, launchd services (whose wrappers source the same file),
    and the desktop app all read the one installed value, and a provider
    switch takes effect everywhere without re-exporting shells. The process
    env var only decides when no installed env file carries a value —
    tests and file-less environments (containers, first-run setup).
    """
    try:
        from openbase_coder_cli.env_file import env_file_values
        from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH

        file_value = (
            env_file_values(DEFAULT_ENV_FILE_PATH)
            .get("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER", "")
            .strip()
            .lower()
        )
    except Exception:  # noqa: BLE001 - unreadable env file -> env fallback
        file_value = ""
    if file_value in PROVIDER_VALUES:
        return file_value
    value = (
        (os.environ.get("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER") or "").strip().lower()
    )
    return value if value in PROVIDER_VALUES else PROVIDER_TAILSCALE


def is_netmesh() -> bool:
    """True for either netmesh transport (VPN or embedded tsnet) — both ride the
    self-hosted headscale control plane and share netmesh labelling/hostnames."""
    return provider() in (PROVIDER_NETMESH, PROVIDER_NETMESH_TSNET)


def netmesh_uses_stock_tailscale() -> bool:
    """True when the netmesh VPN rides the stock tailscale client.

    The hardened netmesh client (netmesh-ctl + root daemon) is macOS-only.
    Windows and Linux join the same self-hosted headscale with the official
    tailscale client (``tailscale login --login-server``), so every netmesh
    control operation on those hosts is a plain tailscale operation.
    """
    import platform as _platform

    return _platform.system() != "Darwin"


def is_netmesh_tsnet() -> bool:
    """True only for the embedded, no-VPN netmesh transport (openbase-tunneld)."""
    return provider() == PROVIDER_NETMESH_TSNET


def tailscale_bin() -> str | None:
    """Locate the official ``tailscale`` CLI (PATH, then known install locations)."""
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in _TAILSCALE_FALLBACK_PATHS:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def netmesh_ctl_bin() -> str | None:
    override = os.environ.get("OPENBASE_CODER_CLI_NETMESH_CTL")
    if override and os.access(override, os.X_OK):
        return override
    # Shipping + dev companion layouts, resolved from the single source in
    # netmesh_companion (so a dev install's companion-build/ shim is found).
    from openbase_coder_cli.services.netmesh_companion import netmesh_ctl_path

    shared = netmesh_ctl_path()
    if shared and os.access(shared, os.X_OK):
        return shared
    # Legacy standalone app (retired) as a last resort.
    for candidate in NETMESH_CTL_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("netmesh-ctl")


def tool_path() -> str | None:
    """Path to the active provider's control tool, or None if not installed."""
    if is_netmesh_tsnet():
        # The embedded no-VPN transport is controlled via openbase-tunneld's
        # loopback API rather than a CLI binary; "installed" means the daemon
        # binary is locatable or a daemon has already run here.
        from openbase_coder_cli.services.tunneld import tunneld_tool_path

        return tunneld_tool_path()
    if is_netmesh() and not netmesh_uses_stock_tailscale():
        return netmesh_ctl_bin()
    return tailscale_bin()


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)


def _parsed(argv: list[str]) -> dict[str, Any]:
    try:
        result = _run(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"Unable to run {argv[0]}: {exc}"}
    if result.returncode != 0:
        return {
            "error": result.stderr.strip() or result.stdout.strip() or "command failed"
        }
    out = result.stdout.strip()
    if not out:
        return {}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"error": f"Unable to parse JSON: {exc}"}
    return payload if isinstance(payload, dict) else {}


def status_json(provider_name: str | None = None) -> dict[str, Any]:
    """Parsed node status (shape matches ``tailscale status --json``: Self, Peer…).

    Routes by the active provider, or by ``provider_name`` when given — a
    transport switch needs the OUTGOING transport's status after the env file
    already carries the new value. Returns a dict with an ``error`` key on
    failure.
    """
    selected = provider_name or provider()
    if selected == PROVIDER_NETMESH_TSNET:
        from openbase_coder_cli.services.tunneld import tunneld_status

        _available, payload, error = tunneld_status()
        if payload is None:
            return {"error": error or "openbase-tunneld status unavailable"}
        return payload
    if selected == PROVIDER_NETMESH and not netmesh_uses_stock_tailscale():
        ctl = netmesh_ctl_bin()
        if not ctl:
            return {"error": "netmesh-ctl not found"}
        return _parsed([ctl, "status"])
    tsc = tailscale_bin()
    if not tsc:
        return {"error": "tailscale was not found on PATH."}
    return _parsed([tsc, "status", "--json"])


def serve_status_json() -> dict[str, Any]:
    """Parsed serve status (shape matches ``tailscale serve status --json``)."""
    if is_netmesh_tsnet():
        # tunneld forwards the serve routes natively; synthesize the shape the
        # tailscale-serve parsers expect from its health payload. (Serve health
        # itself takes a dedicated tunneld path in tailscale_serve.)
        from openbase_coder_cli.services.tunneld import tunneld_health

        health = tunneld_health()
        if not health.get("reachable"):
            return {
                "error": str(health.get("error") or "openbase-tunneld is not reachable")
            }
        if not health.get("forwards_up"):
            return {"error": "openbase-tunneld forwards are not up yet"}
        host = str(health.get("self_dns_name") or "").rstrip(".")
        payload: dict[str, Any] = {
            "TCP": {
                "18080": {"HTTP": True},
                "7880": {"TCPForward": "127.0.0.1:7880"},
            }
        }
        if host:
            payload["Web"] = {
                f"{host}:18080": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:7999"}}}
            }
        return payload
    if is_netmesh() and not netmesh_uses_stock_tailscale():
        ctl = netmesh_ctl_bin()
        if not ctl:
            return {"error": "netmesh-ctl not found"}
        return _parsed([ctl, "serve-status"])
    tsc = tailscale_bin()
    if not tsc:
        return {"error": "tailscale was not found on PATH."}
    return _parsed([tsc, "serve", "status", "--json"])


def _validated_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Validate the narrow Openbase rule vocabulary before it reaches a provider."""
    kind = rule.get("kind")
    if kind in {"openbase-console", "openbase-livekit"}:
        if set(rule) != {"kind"}:
            raise ValueError(f"{kind} does not accept caller-supplied targets.")
        return {"kind": kind}
    if kind == "published-dynamic":
        if set(rule) != {"kind", "tailnet_port", "proxy_port"}:
            raise ValueError("Dynamic publication accepts only validated port fields.")
        tailnet_port = int(rule["tailnet_port"])
        proxy_port = int(rule["proxy_port"])
        if not 49152 <= tailnet_port <= 65535 or not 49152 <= proxy_port <= 65535:
            raise ValueError("Dynamic publication ports must be in 49152-65535.")
        return {
            "kind": kind,
            "tailnet_port": tailnet_port,
            "proxy_port": proxy_port,
        }
    if kind == "published-hostname":
        if set(rule) != {"kind", "hostname", "proxy_port"}:
            raise ValueError(
                "Hostname publication accepts only a validated hostname and proxy port."
            )
        from openbase_coder_cli.services.published_services import validate_hostname

        hostname = validate_hostname(str(rule["hostname"]))
        proxy_port = int(rule["proxy_port"])
        if not 49152 <= proxy_port <= 65535:
            raise ValueError("The hostname proxy port must be in 49152-65535.")
        return {"kind": kind, "hostname": hostname, "proxy_port": proxy_port}
    raise ValueError(f"Unsupported Openbase Serve rule kind: {kind!r}.")


def _validated_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validated = [_validated_rule(rule) for rule in rules]
    kinds = [rule["kind"] for rule in validated]
    for singleton in ("openbase-console", "openbase-livekit"):
        if kinds.count(singleton) > 1:
            raise ValueError(f"Duplicate {singleton} Serve rule.")
    hostnames = [
        str(rule["hostname"])
        for rule in validated
        if rule["kind"] == "published-hostname"
    ]
    if len(hostnames) != len(set(hostnames)):
        raise ValueError("Duplicate private service hostname Serve rule.")
    return validated


def serve_capability() -> dict[str, Any]:
    """Return the hardened helper's declared atomic Serve capability."""
    if is_netmesh_tsnet():
        return {"supported": False, "error": "Openbase Direct is not a host VPN."}
    if not is_netmesh() or netmesh_uses_stock_tailscale():
        return {
            "supported": False,
            "error": "This Openbase VPN client lacks the signed atomic Serve helper.",
        }
    ctl = netmesh_ctl_bin()
    if not ctl:
        return {"supported": False, "error": "netmesh-ctl was not found."}
    payload = _parsed([ctl, "serve-capabilities"])
    if payload.get("error"):
        return {"supported": False, "error": str(payload["error"])}
    return payload


def hostname_serve_capability() -> dict[str, Any]:
    """Compose the helper's routing half with Cloud's DNS-allocation half."""
    capability = serve_capability()
    if not capability.get("supported"):
        return capability
    declared = capability.get("service_hostnames")
    if not isinstance(declared, dict):
        return {
            "supported": False,
            "error": (
                "Openbase VPN does not advertise private service hostname routing."
            ),
        }
    if declared.get("supported") is not True:
        return {
            "supported": False,
            "error": "Openbase VPN explicitly disabled private hostname routing.",
        }
    helper_required = {
        "serve_routing": True,
        "pattern": "{service}.{node_dns_name}",
        "http_port": 80,
    }
    for key, expected in helper_required.items():
        if declared.get(key) != expected:
            return {
                "supported": False,
                "error": f"Openbase VPN private hostname capability lacks {key}={expected!r}.",
            }
    if capability.get("atomic_etag") is not True:
        return {
            "supported": False,
            "error": "Openbase VPN private hostname routing requires atomic ETag apply.",
        }
    from openbase_coder_cli.services.cloud_registration import (
        netmesh_service_hostname_capabilities,
    )

    cloud_result = netmesh_service_hostname_capabilities()
    cloud = cloud_result.response if isinstance(cloud_result.response, dict) else {}
    if not cloud_result.ok or cloud.get("supported") is not True:
        return {
            "supported": False,
            "error": cloud_result.error
            or "Openbase Cloud private hostname DNS allocation is unavailable.",
        }
    cloud_required = {
        "dns_allocation": True,
        "pattern": "{service}.{node_dns_name}",
        "http_port": 80,
    }
    for key, expected in cloud_required.items():
        if cloud.get(key) != expected:
            return {
                "supported": False,
                "error": f"Openbase Cloud private hostname capability lacks {key}={expected!r}.",
            }
    return {
        "supported": True,
        "dns_allocation": True,
        "serve_routing": True,
        "pattern": "{service}.{node_dns_name}",
        "http_port": 80,
    }


def serve_snapshot() -> dict[str, Any]:
    if not is_netmesh() or is_netmesh_tsnet() or netmesh_uses_stock_tailscale():
        raise RuntimeError(
            "Atomic Serve snapshots require the hardened Openbase VPN helper."
        )
    ctl = netmesh_ctl_bin()
    if not ctl:
        raise RuntimeError("netmesh-ctl was not found.")
    payload = _parsed([ctl, "serve-snapshot"])
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    if not isinstance(payload.get("etag"), str) or not isinstance(
        payload.get("hash"), str
    ):
        raise RuntimeError("netmesh-ctl returned an invalid Serve snapshot.")
    return payload


def plan_serve(rules: list[dict[str, Any]]) -> dict[str, Any]:
    validated = _validated_rules(rules)
    if not is_netmesh() or is_netmesh_tsnet() or netmesh_uses_stock_tailscale():
        raise RuntimeError(
            "Atomic Serve planning requires the hardened Openbase VPN helper."
        )
    ctl = netmesh_ctl_bin()
    if not ctl:
        raise RuntimeError("netmesh-ctl was not found.")
    payload = _parsed([ctl, "serve-plan", json.dumps(validated, separators=(",", ":"))])
    if payload.get("error") or not isinstance(payload.get("hash"), str):
        raise RuntimeError(str(payload.get("error") or "Invalid Serve plan response."))
    return payload


def _legacy_cli_rule(rule: dict[str, Any]) -> tuple[str, int, str]:
    kind = rule["kind"]
    if kind == "openbase-console":
        return "http", 18080, "http://127.0.0.1:7999"
    if kind == "openbase-livekit":
        return "tcp", 7880, "tcp://127.0.0.1:7880"
    if kind == "published-dynamic":
        return (
            "http",
            int(rule["tailnet_port"]),
            f"http://127.0.0.1:{rule['proxy_port']}",
        )
    raise RuntimeError("Hostname publication is unavailable through this provider.")


def apply_serve_legacy(rules: list[dict[str, Any]]) -> None:
    """Compatibility path for the pre-CAS signed helper, never for hostnames."""
    if not is_netmesh() or is_netmesh_tsnet() or netmesh_uses_stock_tailscale():
        raise RuntimeError(
            "Legacy Serve replacement is only available to Openbase VPN."
        )
    ctl = netmesh_ctl_bin()
    if not ctl:
        raise RuntimeError("netmesh-ctl was not found.")
    legacy_rules = []
    for rule in _validated_rules(rules):
        proto, port, target = _legacy_cli_rule(rule)
        legacy_rules.append({"proto": proto, "port": port, "target": target})
    result = _run([ctl, "serve-set", json.dumps(legacy_rules, separators=(",", ":"))])
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or "Legacy Openbase VPN serve-set failed."
        )


def apply_serve(
    rules: list[dict[str, Any]],
    *,
    expected_etag: str | None = None,
    expected_hash: str | None = None,
) -> dict[str, Any]:
    """Apply validated Openbase Serve rules.

    Raises ``RuntimeError`` on failure.
    """
    validated = _validated_rules(rules)
    if is_netmesh_tsnet():
        # The daemon forwards 18080/7880/7881 itself; "applying serve" means
        # making sure it is running and logged in.
        from openbase_coder_cli.services.tunneld import ensure_tunneld_running

        ensure_tunneld_running()
        return {}
    if is_netmesh() and not netmesh_uses_stock_tailscale():
        if expected_etag is None or expected_hash is None:
            raise RuntimeError(
                "Atomic Serve apply requires an ETag and expected config hash."
            )
        ctl = netmesh_ctl_bin()
        if not ctl:
            raise RuntimeError("netmesh-ctl was not found.")
        payload = _parsed(
            [
                ctl,
                "serve-apply",
                json.dumps(validated, separators=(",", ":")),
                expected_etag,
                expected_hash,
            ]
        )
        if payload.get("error") or not isinstance(payload.get("hash"), str):
            raise RuntimeError(
                str(payload.get("error") or "Atomic Serve apply failed.")
            )
        return payload
    tsc = tailscale_bin()
    if not tsc:
        raise RuntimeError("tailscale was not found.")
    for rule in validated:
        proto, port, target = _legacy_cli_rule(rule)
        flag = f"--{proto}={port}"
        result = _run([tsc, "serve", "--bg", flag, target])
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip()
                or result.stdout.strip()
                or "tailscale serve failed."
            )
    return {}


def remove_serve(proto: str, port: int) -> None:
    """Remove one official/stock Tailscale Serve listener.

    Hardened Netmesh replaces its whole vetted Serve configuration through
    ``apply_serve`` instead, so callers must rebuild that provider's rules.
    """
    if is_netmesh_tsnet():
        raise RuntimeError("Openbase Direct does not expose arbitrary Serve routes.")
    if is_netmesh() and not netmesh_uses_stock_tailscale():
        raise RuntimeError("Hardened Netmesh Serve routes must be replaced as a set.")
    tsc = tailscale_bin()
    if not tsc:
        raise RuntimeError("tailscale was not found.")
    result = _run([tsc, "serve", f"--{proto}={port}", "off"])
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or "tailscale serve off failed."
        )
