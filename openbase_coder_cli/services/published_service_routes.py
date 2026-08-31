from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openbase_coder_cli.services import tailscale_provider as tp

if TYPE_CHECKING:
    from openbase_coder_cli.services.published_services import PublishedService


def ensure_portless_capability() -> None:
    """Reject unsupported providers before registry or process state is changed."""
    from openbase_coder_cli.services.published_services import PORTLESS_TAILNET_PORT

    if tp.is_netmesh_tsnet():
        raise RuntimeError(
            "Openbase Direct cannot publish portless host services. "
            "Switch this computer to Openbase VPN first."
        )
    if not tp.is_netmesh():
        raise RuntimeError(
            "Portless publication requires Openbase VPN; the official Tailscale "
            "provider is not supported."
        )
    capability = tp.portless_serve_capability()
    if not capability.get("supported"):
        raise RuntimeError(
            str(
                capability.get("error")
                or "The active Openbase VPN helper lacks atomic portless Serve support."
            )
        )
    if int(capability.get("http_port") or 0) != PORTLESS_TAILNET_PORT:
        raise RuntimeError(
            "The active Openbase VPN helper did not authorize HTTP port 80."
        )
    if capability.get("atomic_etag") is not True:
        raise RuntimeError(
            "The active Openbase VPN helper does not support ETag-protected atomic apply."
        )
    if capability.get("cert_domains"):
        raise RuntimeError(
            "Portless v1 expects no certificate domains and uses "
            "WireGuard-encrypted HTTP port 80."
        )


def _desired_rules(services: list[PublishedService]) -> list[dict[str, Any]]:
    from openbase_coder_cli.services.published_services import MODE_PORTLESS
    from openbase_coder_cli.services.tailscale_serve import openbase_serve_rules

    rules = list(openbase_serve_rules())
    seen_portless = False
    for service in services:
        if service.mode == MODE_PORTLESS:
            if seen_portless:
                continue
            seen_portless = True
        rules.append(service.serve_rule())
    return rules


def reconcile_openbase_routes(
    previous_services: list[PublishedService],
    desired_services: list[PublishedService],
    last_applied_hash: str | None,
) -> str:
    """CAS-replace the complete Openbase-owned Serve config on hardened Netmesh."""
    snapshot = tp.serve_snapshot()
    previous_plan = tp.plan_serve(_desired_rules(previous_services))
    expected_hash = last_applied_hash or str(previous_plan["hash"])
    if snapshot.get("hash") != expected_hash:
        raise RuntimeError(
            "Openbase VPN Serve configuration drifted from the last known desired "
            "state; refusing to overwrite unknown routes."
        )
    result = tp.apply_serve(
        _desired_rules(desired_services),
        expected_etag=str(snapshot["etag"]),
        expected_hash=expected_hash,
    )
    applied_hash = result.get("hash") if isinstance(result, dict) else None
    if not isinstance(applied_hash, str) or not applied_hash:
        raise RuntimeError(
            "Openbase VPN helper did not confirm the applied Serve hash."
        )
    return applied_hash


def apply_route(
    service: PublishedService,
    *,
    previous_services: list[PublishedService] | None = None,
    desired_services: list[PublishedService] | None = None,
    last_applied_hash: str | None = None,
) -> str | None:
    from openbase_coder_cli.services.published_services import (
        MODE_PORTLESS,
        load_services,
    )

    if tp.is_netmesh_tsnet():
        raise RuntimeError(
            "Openbase Direct cannot publish arbitrary host services. "
            "Switch this computer to Openbase VPN first."
        )
    if service.mode == MODE_PORTLESS:
        ensure_portless_capability()
    if tp.is_netmesh() and not tp.netmesh_uses_stock_tailscale():
        if not tp.portless_serve_capability().get("supported"):
            tp.apply_serve_legacy(_desired_rules(desired_services or load_services()))
            return None
        return reconcile_openbase_routes(
            previous_services or [],
            desired_services or load_services(),
            last_applied_hash,
        )
    tp.apply_serve([service.serve_rule()])
    return None


def remove_route(
    service: PublishedService,
    *,
    previous_services: list[PublishedService] | None = None,
    desired_services: list[PublishedService] | None = None,
    last_applied_hash: str | None = None,
) -> str | None:
    from openbase_coder_cli.services.published_services import (
        MODE_PORTLESS,
        load_services,
    )

    if service.mode == MODE_PORTLESS:
        ensure_portless_capability()

    if tp.is_netmesh() and not tp.netmesh_uses_stock_tailscale():
        if not tp.portless_serve_capability().get("supported"):
            tp.apply_serve_legacy(_desired_rules(desired_services or load_services()))
            return None
        return reconcile_openbase_routes(
            previous_services or load_services(),
            desired_services or load_services(),
            last_applied_hash,
        )
    tp.remove_serve("http", service.tailnet_port)
    return None
