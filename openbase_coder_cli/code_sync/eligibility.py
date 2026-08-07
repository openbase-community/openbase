"""Code-sync eligibility from the cloud device registry.

The feature only arms when the account has two or more non-phone devices
with Tailscale identities. Phones never participate as sync peers; they only
view sync state and conflicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
    TokenManager,
)
from openbase_coder_cli.services.cloud_registration import local_device_id
from openbase_coder_cli.services.onboarding import web_backend_url

ONBOARDING_STATE_PATH = "/api/openbase/onboarding/state/"
DEVICE_DEREGISTER_PATH = "/api/openbase/devices/deregister/"
REQUEST_TIMEOUT_SECONDS = 15
MIN_SYNC_DEVICES = 2


@dataclass(frozen=True)
class SyncPeer:
    device_id: str
    name: str
    kind: str
    tailscale_magic_dns: str
    syncthing_device_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "kind": self.kind,
            "tailscale_magic_dns": self.tailscale_magic_dns,
            "syncthing_device_id": self.syncthing_device_id,
        }


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reason: str
    peers: tuple[SyncPeer, ...] = ()


def evaluate_state(state: dict[str, Any], self_device_id: str) -> EligibilityResult:
    """Evaluate a ``GET /api/openbase/onboarding/state/`` payload."""
    devices = state.get("devices")
    if not isinstance(devices, list):
        return EligibilityResult(
            eligible=False, reason="Cloud device registry returned no devices."
        )

    sync_capable = [
        device
        for device in devices
        if isinstance(device, dict)
        and str(device.get("kind") or "") != "mobile"
        and str(device.get("tailscale_magic_dns") or "").strip()
    ]
    peers = tuple(
        _peer_from_device(device)
        for device in sync_capable
        if str(device.get("device_id") or "") != self_device_id
    )
    if len(sync_capable) < MIN_SYNC_DEVICES:
        return EligibilityResult(
            eligible=False,
            reason=(
                "Code sync needs at least two non-phone devices with "
                f"Tailscale identities; found {len(sync_capable)}. "
                "Add a second machine to enable sync."
            ),
            peers=peers,
        )
    return EligibilityResult(eligible=True, reason="", peers=peers)


def _peer_from_device(device: dict[str, Any]) -> SyncPeer:
    capabilities = device.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}
    return SyncPeer(
        device_id=str(device.get("device_id") or ""),
        name=str(device.get("display_name") or device.get("hostname") or "unknown"),
        kind=str(device.get("kind") or ""),
        tailscale_magic_dns=str(device.get("tailscale_magic_dns") or "").strip(),
        syncthing_device_id=str(capabilities.get("syncthing_device_id") or "").strip(),
    )


def fetch_cloud_state() -> dict[str, Any]:
    """GET the signed-in user's registered devices. Raises on failure."""
    backend_url = web_backend_url()
    token = TokenManager(backend_url).get_access_token()
    response = httpx.get(
        f"{backend_url}{ONBOARDING_STATE_PATH}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Onboarding state endpoint returned a non-object.")
    return payload


def deregister_cloud_device(device_id: str) -> int:
    """Forget a device from the signed-in user's cloud registry.

    Removing the device from the registry is the single source of truth for
    sync peers: on the next reconcile it drops out of the eligibility peer
    list and the Syncthing configuration, which clears the offline
    "peer disconnected" warning for a machine the user has shut down.

    Returns the number of registry entries deleted. Raises on failure.
    """
    device_id = (device_id or "").strip()
    if not device_id:
        raise ValueError("A device_id is required to deregister a sync peer.")
    backend_url = web_backend_url()
    token = TokenManager(backend_url).get_access_token()
    response = httpx.post(
        f"{backend_url}{DEVICE_DEREGISTER_PATH}",
        headers={"Authorization": f"Bearer {token}"},
        json={"device_id": device_id},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Device deregister endpoint returned a non-object.")
    return int(payload.get("deleted_count") or 0)


def current_eligibility() -> EligibilityResult:
    """Fetch and evaluate eligibility, degrading to a reason on failure."""
    try:
        state = fetch_cloud_state()
    except AuthLoginRequiredError:
        return EligibilityResult(
            eligible=False,
            reason="Login required. Run 'openbase-coder login' first.",
        )
    except (AuthTransientError, httpx.HTTPError, ValueError) as exc:
        return EligibilityResult(
            eligible=False, reason=f"Cloud device registry unreachable: {exc}"
        )
    return evaluate_state(state, local_device_id())


def syncable_peers(result: EligibilityResult) -> tuple[SyncPeer, ...]:
    """Peers that can appear in the Syncthing config (advertised device ID).

    The cloud registry can hold stale records — a reinstalled machine's old
    device entry, or duplicates of a peer. Deduplicate by Syncthing device
    ID and never treat our own engine identity as a peer: a phantom
    self-peer triples reconcile passes and reconciles repos against
    themselves.
    """
    from openbase_coder_cli.code_sync.syncthing import stored_device_id

    own_engine_id = stored_device_id() or ""
    seen: set[str] = set()
    peers: list[SyncPeer] = []
    for peer in result.peers:
        engine_id = peer.syncthing_device_id
        if not engine_id or engine_id == own_engine_id or engine_id in seen:
            continue
        seen.add(engine_id)
        peers.append(peer)
    return tuple(peers)
