from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx

OPENBASE_CODER_TAILNET_PORT = 18080
OPENBASE_HEALTH_PATH = "/api/health/"
TAILSCALE_STATUS_TIMEOUT_SECONDS = 5
OPENBASE_PROBE_TIMEOUT_SECONDS = 0.8
OPENBASE_PROBE_WORKERS = 16


@dataclass
class TailnetDevice:
    name: str
    host: str
    dns_name: str | None
    ip: str | None
    online: bool
    os: str | None
    openbase_url: str | None = None
    openbase_available: bool = False
    probe_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "host": self.host,
            "dns_name": self.dns_name,
            "ip": self.ip,
            "online": self.online,
            "os": self.os,
            "openbase_url": self.openbase_url,
            "openbase_available": self.openbase_available,
            "probe_error": self.probe_error,
        }


def _tailscale_status_payload() -> tuple[bool, dict[str, Any] | None, str | None]:
    """Run ``tailscale status --json``.

    Returns ``(tailscale_available, status_payload, error)``; ``status_payload``
    is ``None`` whenever ``error`` is set.
    """
    from openbase_coder_cli.services import tailscale_provider as tp

    if tp.tool_path() is None:
        return False, None, f"{tp.provider()} control tool was not found."

    status = tp.status_json()
    if "error" in status:
        return True, None, str(status["error"])
    return True, status, None


def tailscale_self_identity() -> dict[str, Any]:
    """Return the local node's Tailscale identity for device registration."""
    tailscale_available, status_payload, error = _tailscale_status_payload()
    identity: dict[str, Any] = {
        "available": False,
        "tailscale_available": tailscale_available,
        "dns_name": None,
        "node_hostname": None,
        "tailnet": None,
        "ips": [],
        "error": error,
    }
    if status_payload is None:
        return identity

    self_entry = status_payload.get("Self")
    if not isinstance(self_entry, dict):
        identity["error"] = "tailscale status did not include a Self entry."
        return identity

    dns_name = _normalize_dns_name(self_entry.get("DNSName"))
    ips = self_entry.get("TailscaleIPs")
    tailnet = status_payload.get("CurrentTailnet")
    tailnet_name = (
        tailnet.get("MagicDNSSuffix") or tailnet.get("Name")
        if isinstance(tailnet, dict)
        else None
    ) or status_payload.get("MagicDNSSuffix")

    identity.update(
        {
            "available": bool(dns_name),
            "dns_name": dns_name,
            "node_hostname": str(self_entry.get("HostName") or "") or None,
            "tailnet": _normalize_dns_name(tailnet_name),
            "ips": [str(ip) for ip in ips] if isinstance(ips, list) else [],
            "error": None if dns_name else "Tailscale DNS name is unavailable.",
        }
    )
    return identity


def tailnet_devices_payload() -> dict[str, Any]:
    tailscale_available, status_payload, error = _tailscale_status_payload()
    if status_payload is None:
        return {
            "tailscale_available": tailscale_available,
            "devices": [],
            "openbase_devices": [],
            "error": error,
        }

    devices = _devices_from_tailscale_status(status_payload)
    _probe_openbase_devices(devices)
    device_payloads = [device.to_dict() for device in devices]

    return {
        "tailscale_available": True,
        "devices": device_payloads,
        "openbase_devices": [
            device for device in device_payloads if device["openbase_available"]
        ],
        "error": None,
    }


def _devices_from_tailscale_status(payload: dict[str, Any]) -> list[TailnetDevice]:
    devices: list[TailnetDevice] = []

    self_device = payload.get("Self")
    if isinstance(self_device, dict):
        device = _device_from_status_entry(self_device, online=True)
        if device is not None:
            devices.append(device)

    peers = payload.get("Peer")
    if isinstance(peers, dict):
        for peer in peers.values():
            if not isinstance(peer, dict):
                continue
            device = _device_from_status_entry(peer)
            if device is not None:
                devices.append(device)

    deduped: dict[str, TailnetDevice] = {}
    for device in devices:
        key = device.dns_name or device.ip or device.host
        deduped.setdefault(key, device)
    return sorted(deduped.values(), key=lambda device: device.name.lower())


def _device_from_status_entry(
    entry: dict[str, Any],
    *,
    online: bool | None = None,
) -> TailnetDevice | None:
    ips = entry.get("TailscaleIPs")
    ip = str(ips[0]) if isinstance(ips, list) and ips else None

    dns_name = _normalize_dns_name(entry.get("DNSName"))
    host = dns_name or ip
    if not host:
        return None

    name = str(entry.get("HostName") or host).strip() or host
    online_value = online if online is not None else bool(entry.get("Online"))
    os_name = entry.get("OS")

    return TailnetDevice(
        name=name,
        host=host,
        dns_name=dns_name,
        ip=ip,
        online=online_value,
        os=str(os_name) if os_name else None,
    )


def _normalize_dns_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip().rstrip(".")
    return name or None


def _probe_openbase_devices(devices: list[TailnetDevice]) -> None:
    online_devices: list[TailnetDevice] = []
    for device in devices:
        if device.online:
            online_devices.append(device)
        else:
            device.probe_error = "offline"

    if not online_devices:
        return

    worker_count = min(OPENBASE_PROBE_WORKERS, len(online_devices))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        list(executor.map(_probe_openbase_device, online_devices))


def _probe_openbase_device(device: TailnetDevice) -> None:
    url = f"http://{_url_host_literal(device.host)}:{OPENBASE_CODER_TAILNET_PORT}{OPENBASE_HEALTH_PATH}"
    device.openbase_url = url.removesuffix(OPENBASE_HEALTH_PATH)
    try:
        response = httpx.get(url, timeout=OPENBASE_PROBE_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        device.probe_error = str(exc)
        return

    if response.status_code != 200:
        device.probe_error = f"HTTP {response.status_code}"
        return

    try:
        payload = response.json()
    except ValueError:
        device.probe_error = "Invalid JSON response"
        return

    if payload.get("status") != "ok":
        device.probe_error = "Unexpected health response"
        return

    device.openbase_available = True
    device.probe_error = None


def _url_host_literal(host: str) -> str:
    if ":" in host and not host.startswith("[") and not host.endswith("]"):
        return f"[{host}]"
    return host
