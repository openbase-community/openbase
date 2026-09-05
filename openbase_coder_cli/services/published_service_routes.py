from __future__ import annotations

import ipaddress
import socket
import time
from typing import TYPE_CHECKING, Any, NamedTuple

from openbase_coder_cli.services import tailscale_provider as tp

if TYPE_CHECKING:
    from openbase_coder_cli.services.published_services import PublishedService

# A fresh allocation's MagicDNS record takes several seconds to propagate
# (~10s observed on staging), so resolution is polled rather than checked once.
HOSTNAME_DNS_TIMEOUT_SECONDS = 30.0
HOSTNAME_DNS_POLL_SECONDS = 2.0


class HostnamePublicationUnavailable(RuntimeError):
    """The provider safely supports dynamic ports but not private hostnames."""


class ServiceHostnameAllocation(NamedTuple):
    hostname: str
    node_id: str
    created: bool


def allocate_private_service_hostname(name: str) -> ServiceHostnameAllocation:
    """Allocate and verify a private service hostname for this exact node.

    Cloud verifies node ownership and writes Headscale's private DNS record.
    This process then verifies that the record resolves to the local node before
    returning it to the publication transaction.
    """
    from openbase_coder_cli.services.published_services import (
        HOSTNAME_TAILNET_PORT,
        validate_hostname,
    )

    if tp.is_netmesh_tsnet():
        raise HostnamePublicationUnavailable(
            "Openbase Direct cannot publish private host services. "
            "Switch this computer to Openbase VPN first."
        )
    if not tp.is_netmesh():
        raise HostnamePublicationUnavailable(
            "Private hostname publication requires Openbase VPN; the official Tailscale "
            "provider is not supported."
        )
    capability = tp.hostname_serve_capability()
    if not capability.get("supported"):
        raise HostnamePublicationUnavailable(
            str(
                capability.get("error")
                or "Openbase VPN lacks private service hostname support."
            )
        )
    if int(capability.get("http_port") or 0) != HOSTNAME_TAILNET_PORT:
        raise RuntimeError(
            "The active Openbase VPN helper did not authorize HTTP port 80."
        )
    node_name, node_ips = _self_node_identity()
    from openbase_coder_cli.services.cloud_registration import (
        allocate_netmesh_service_hostname,
        list_netmesh_devices,
    )

    matching_nodes = []
    for node in list_netmesh_devices():
        cloud_ips = set()
        for value in node.get("ip_addresses") or []:
            try:
                cloud_ips.add(str(ipaddress.ip_address(str(value))))
            except ValueError:
                continue
        given_name = str(node.get("given_name") or node.get("name") or "").lower()
        if (
            node_ips.isdisjoint(cloud_ips)
            or not given_name
            or not node_name.startswith(f"{given_name}.")
        ):
            continue
        matching_nodes.append(node)
    if len(matching_nodes) != 1 or not matching_nodes[0].get("id"):
        raise HostnamePublicationUnavailable(
            "Openbase Cloud could not identify this VPN node unambiguously."
        )
    node_id = str(matching_nodes[0]["id"])
    allocation_result = allocate_netmesh_service_hostname(
        node_id=node_id, service_name=name
    )
    allocation = (
        allocation_result.response
        if isinstance(allocation_result.response, dict)
        else {}
    )
    if not allocation_result.ok:
        raise HostnamePublicationUnavailable(
            allocation_result.error or "Private hostname allocation failed."
        )
    created = allocation.get("created") is True
    try:
        expected_hostname = validate_hostname(f"{name}.{node_name}")
        hostname = validate_hostname(str(allocation.get("hostname") or ""))
    except ValueError as exc:
        _rollback_hostname_allocation(name, node_id, created)
        raise RuntimeError(
            "Openbase Cloud returned an invalid private hostname."
        ) from exc
    if hostname != expected_hostname or str(allocation.get("node_id")) != node_id:
        _rollback_hostname_allocation(name, node_id, created)
        raise RuntimeError(
            "Openbase Cloud returned a private hostname outside this node's allocation."
        )
    try:
        _await_hostname_resolution(hostname, node_ips)
    except HostnamePublicationUnavailable:
        _rollback_hostname_allocation(name, node_id, created)
        raise
    return ServiceHostnameAllocation(hostname, node_id, created)


def _await_hostname_resolution(hostname: str, node_ips: set[str]) -> None:
    """Poll DNS until the freshly written record resolves to this node."""
    from openbase_coder_cli.services.published_services import HOSTNAME_TAILNET_PORT

    deadline = time.monotonic() + HOSTNAME_DNS_TIMEOUT_SECONDS
    while True:
        resolution_error: Exception | None = None
        resolved_ips: set[str] = set()
        try:
            resolved_ips = {
                str(ipaddress.ip_address(address[4][0]))
                for address in socket.getaddrinfo(
                    hostname, HOSTNAME_TAILNET_PORT, type=socket.SOCK_STREAM
                )
            }
        except (OSError, ValueError) as exc:
            resolution_error = exc
        if resolved_ips and not node_ips.isdisjoint(resolved_ips):
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(HOSTNAME_DNS_POLL_SECONDS)
    waited = f"within {HOSTNAME_DNS_TIMEOUT_SECONDS:.0f}s"
    if resolution_error is not None:
        raise HostnamePublicationUnavailable(
            f"Openbase VPN advertised {hostname}, but DNS did not resolve it "
            f"{waited}: {resolution_error}"
        ) from resolution_error
    raise HostnamePublicationUnavailable(
        f"Openbase VPN advertised {hostname}, but DNS did not resolve it to this "
        f"node {waited}."
    )


def verify_private_service_hostname(name: str, hostname: str) -> None:
    node_name, node_ips = _self_node_identity()
    from openbase_coder_cli.services.published_services import validate_hostname

    if validate_hostname(hostname) != validate_hostname(f"{name}.{node_name}"):
        raise RuntimeError(
            "The stored private service hostname does not belong to this VPN node."
        )
    resolved_ips = {
        str(ipaddress.ip_address(address[4][0]))
        for address in socket.getaddrinfo(hostname, 80, type=socket.SOCK_STREAM)
    }
    if node_ips.isdisjoint(resolved_ips):
        raise RuntimeError(
            "The stored private service hostname does not resolve to this VPN node."
        )


def release_private_service_hostname(name: str, node_id: str) -> None:
    from openbase_coder_cli.services.cloud_registration import (
        release_netmesh_service_hostname,
    )

    result = release_netmesh_service_hostname(node_id=node_id, service_name=name)
    if not result.ok:
        raise RuntimeError(result.error or "Private hostname release failed.")


def _rollback_hostname_allocation(name: str, node_id: str, created: bool) -> None:
    if not created:
        return
    try:
        release_private_service_hostname(name, node_id)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Private hostname verification failed and cleanup also failed: {exc}"
        ) from exc


def _self_node_identity() -> tuple[str, set[str]]:
    status = tp.status_json()
    if status.get("error"):
        raise RuntimeError(str(status["error"]))
    self_status = status.get("Self") if isinstance(status.get("Self"), dict) else {}
    node_name = str(self_status.get("DNSName") or "").strip().lower().rstrip(".")
    if not node_name:
        raise RuntimeError(
            "Openbase VPN did not report the node DNS name needed for a private hostname."
        )
    node_ips = set()
    for value in self_status.get("TailscaleIPs") or []:
        try:
            node_ips.add(str(ipaddress.ip_address(str(value))))
        except ValueError:
            continue
    if not node_ips:
        raise RuntimeError("Openbase VPN did not report this node's tailnet address.")
    return node_name, node_ips


def _desired_rules(services: list[PublishedService]) -> list[dict[str, Any]]:
    from openbase_coder_cli.services.tailscale_serve import openbase_serve_rules

    rules = list(openbase_serve_rules())
    for service in services:
        rules.append(service.serve_rule())
    return rules


def expected_serve_base_hash(
    snapshot_hash: str,
    previous_rules: list[dict[str, Any]],
    last_applied_hash: str | None,
) -> str:
    """The helper config hash a CAS Serve apply must be based on.

    With no recorded last-applied hash, a helper still reporting its initial
    empty config (fresh install, nothing to overwrite) is as valid a base as
    the baseline plan; the empty-config hash is the helper's own plan of zero
    rules rather than a hardcoded digest.
    """
    if last_applied_hash:
        return last_applied_hash
    expected = str(tp.plan_serve(previous_rules)["hash"])
    if snapshot_hash != expected:
        empty_hash = str(tp.plan_serve([])["hash"])
        if snapshot_hash == empty_hash:
            return empty_hash
    return expected


def reconcile_openbase_routes(
    previous_services: list[PublishedService],
    desired_services: list[PublishedService],
    last_applied_hash: str | None,
) -> str:
    """CAS-replace the complete Openbase-owned Serve config on hardened Netmesh."""
    snapshot = tp.serve_snapshot()
    expected_hash = expected_serve_base_hash(
        str(snapshot.get("hash")),
        _desired_rules(previous_services),
        last_applied_hash,
    )
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
        MODE_HOSTNAME,
        load_services,
    )

    if tp.is_netmesh_tsnet():
        raise RuntimeError(
            "Openbase Direct cannot publish arbitrary host services. "
            "Switch this computer to Openbase VPN first."
        )
    if service.mode == MODE_HOSTNAME:
        if not service.hostname:
            raise RuntimeError("Private hostname publication is missing its hostname.")
        verify_private_service_hostname(service.name, service.hostname)
    if tp.is_netmesh() and not tp.netmesh_uses_stock_tailscale():
        if not tp.serve_capability().get("supported"):
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
    from openbase_coder_cli.services.published_services import load_services

    if tp.is_netmesh() and not tp.netmesh_uses_stock_tailscale():
        if not tp.serve_capability().get("supported"):
            tp.apply_serve_legacy(_desired_rules(desired_services or load_services()))
            return None
        return reconcile_openbase_routes(
            previous_services or load_services(),
            desired_services or load_services(),
            last_applied_hash,
        )
    tp.remove_serve("http", service.tailnet_port)
    return None
