"""Pair computers for Openbase Sync, shared by the console and the CLI.

The console Sync page and ``openbase-coder sync-daemon pair`` both call these
functions, so there is one code path:

- ``candidates()`` lists the user's other computers on Openbase VPN with
  their sync role.
- ``become_hub()`` makes this computer the hub (the always-on computer).
- ``join_hub(hub)`` makes this computer an edge of that hub. The hub hands
  over its pair secret, ports and roots through ``offer()``.
- ``leave()`` stops syncing here; the config is moved to the trash.
- ``add_root()`` / ``remove_root()`` change the synced folders here and on
  the paired computer(s).

Computers talk to each other exactly like fleet aggregation does: over the
tailnet, at the Openbase port, with the owner JWT that every desktop of the
same account accepts. No new trust is introduced: only a computer signed in
to the same account can ask a hub for its offer.

The pair secret travels only in the hub's POST offer response and in the
config file. It is never returned by a GET and never logged.
"""

from __future__ import annotations

import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from openbase_coder_cli import sync_daemon
from openbase_coder_cli.paths import OPENBASE_BASE_DIR
from openbase_coder_cli.services import fleet_aggregation as fleet

logger = logging.getLogger(__name__)

SETTINGS_PATH = "/api/sync/daemon/settings/"
OFFER_PATH = "/api/sync/daemon/pairing/offer/"
ROOTS_PATH = "/api/sync/daemon/roots/"
PROBE_TIMEOUT_SECONDS = 2.5
PAIR_TIMEOUT_SECONDS = 10.0
DEFAULT_PROJECTS_ROOT = "~/Projects"
# The edge (the laptop) holds every file in full; the always-on hub keeps
# large files as placeholders until they are used. Both sides must agree, so
# the hub's choice travels in the offer.
DEFAULT_ANCHOR = "edge"
MOBILE_OS = {"ios", "android"}

_lock = threading.Lock()


class PairingError(RuntimeError):
    """A pairing step failed; ``code`` is stable, the message is for people."""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status

    def to_dict(self) -> dict[str, str]:
        return {"error": str(self), "code": self.code}


# --- this computer ---------------------------------------------------------


def _summary() -> dict[str, Any]:
    return sync_daemon.read_config_summary()


def _role(summary: dict[str, Any]) -> str:
    role = summary.get("role")
    return role if summary.get("configured") and role in {"hub", "edge"} else "none"


def _self_hosts() -> set[str]:
    """This computer's tailnet names and addresses (lowercase)."""
    from openbase_coder_cli.services.tailnet_devices import tailscale_self_identity

    try:
        identity = tailscale_self_identity()
    except Exception:  # noqa: BLE001 - identity is a display nicety
        return set()
    hosts = {str(ip).lower() for ip in identity.get("ips") or []}
    if identity.get("dns_name"):
        hosts.add(str(identity["dns_name"]).lower())
    return hosts


def split_host_port(address: str) -> tuple[str, int | None]:
    """``host:port`` / ``[v6]:port`` / ``host`` -> (host, port)."""
    address = (address or "").strip()
    if address.startswith("["):
        host, _, rest = address[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port) if port.isdigit() else None
    if address.count(":") == 1:
        host, _, port = address.partition(":")
        return host, int(port) if port.isdigit() else None
    return address, None


def _address(host: str, port: int) -> str:
    literal = f"[{host}]" if ":" in host else host
    return f"{literal}:{port}"


def daemon_binary_available() -> bool:
    """Whether the ``openbase-syncd`` binary the service runs can be found."""
    try:
        from openbase_coder_cli.services.installation import InstallationConfig
        from openbase_coder_cli.services.launchd import _binary_resolvers

        return bool(_binary_resolvers(InstallationConfig.load())["openbase_syncd"]())
    except Exception:  # noqa: BLE001 - any failure means "not available"
        return False


def _require_daemon_binary() -> None:
    if not daemon_binary_available():
        raise PairingError(
            "binaries_missing",
            "The Openbase Sync daemon is not installed on this computer. "
            "Update Openbase and try again.",
            409,
        )


def _require_unconfigured() -> None:
    summary = _summary()
    if not summary.get("configured"):
        return
    if summary.get("role") == "hub":
        raise PairingError(
            "already_hub", "This computer is already the always-on computer.", 409
        )
    raise PairingError(
        "already_configured",
        "This computer already syncs. Stop syncing on it first.",
        409,
    )


def default_roots() -> list[str]:
    """``~/Projects`` plus the folders thread sync and skills sync use."""
    roots = [DEFAULT_PROJECTS_ROOT]
    for root in sync_daemon.product_folder_roots():
        if root not in roots:
            roots.append(root)
    return roots


def check_root_allowed(path: str) -> None:
    """Refuse folders that must never be mirrored.

    The whole home folder (or anything above it) would carry credentials and
    every app's state; the sync daemon's own state directory must never sync
    itself. Product folders inside ``~/.openbase`` (thread sync) stay allowed.
    """
    target = sync_daemon.expand_root_path(path)
    home = Path.home().resolve()
    if target == home or target in home.parents:
        raise PairingError(
            "root_not_allowed",
            f"{path} is too broad. Choose a folder inside your home folder.",
            400,
        )
    base = sync_daemon.expand_root_path(sync_daemon.OPENBASE_BASE_DIR)
    state = sync_daemon.expand_root_path(sync_daemon.SYNC_DAEMON_CONFIG_PATH.parent)
    if target == base or target == state or state in target.parents:
        raise PairingError(
            "root_not_allowed",
            f"{path} holds Openbase's own settings and cannot be synced.",
            400,
        )


def _ensure_root_dirs(roots: list[dict[str, Any]]) -> None:
    for root in roots:
        sync_daemon.expand_root_path(root["path"]).mkdir(parents=True, exist_ok=True)


def _service():
    from openbase_coder_cli.services.registry import find_service

    return find_service(sync_daemon.SYNC_DAEMON_SERVICE_NAME)


def _start_service() -> None:
    from openbase_coder_cli.services.installation import InstallationConfig
    from openbase_coder_cli.services.launchd import install_service

    try:
        install_service(InstallationConfig.load(), _service())
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear message
        raise PairingError(
            "service_failed",
            f"Saved the sync setup, but the sync service did not start ({exc}). "
            "Start it with 'openbase-coder services start sync-daemon'.",
            500,
        ) from exc


def _stop_service() -> None:
    from openbase_coder_cli.services.launchd import remove_service

    try:
        remove_service(_service())
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear message
        raise PairingError(
            "service_failed",
            f"Could not stop the sync service ({exc}). Nothing was changed.",
            500,
        ) from exc


def refresh_cloud_registration(*, background: bool = True) -> None:
    """Tell Openbase Cloud this computer's new sync role; best effort."""

    def run() -> None:
        from openbase_coder_cli.services.cloud_registration import (
            register_and_report,
        )

        try:
            register_and_report()
        except Exception:  # noqa: BLE001 - retried at the next periodic check-in
            logger.info("sync_pairing_cloud_registration_failed", exc_info=True)

    if background:
        threading.Thread(target=run, name="sync-pairing-register", daemon=True).start()
    else:
        run()


# --- peers -----------------------------------------------------------------


def _pairing_peers() -> list[fleet.FleetPeer]:
    """The user's other online computers (phones excluded)."""
    return [
        peer
        for peer in fleet.fleet_peers(include_failed=True)
        if (peer.os or "").lower() not in MOBILE_OS
    ]


def _peer_matches(peer: fleet.FleetPeer, value: str) -> bool:
    value = value.strip().lower()
    return bool(value) and value in {
        peer.key.lower(),
        peer.name.lower(),
        (peer.ip or "").lower(),
    }


def find_pairing_peer(value: str) -> fleet.FleetPeer | None:
    for peer in _pairing_peers():
        if _peer_matches(peer, value):
            return peer
    return None


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _require_token() -> str:
    token = fleet.owner_access_token()
    if not token:
        raise PairingError(
            "not_signed_in", "Sign in to Openbase on this computer first.", 409
        )
    return token


def _probe(peer: fleet.FleetPeer, token: str | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": peer.key,
        "name": peer.name,
        "host": peer.key,
        "reachable": False,
        "role": "unknown",
        "hub_host": None,
        "error": None,
    }
    if token is None:
        entry["error"] = "Sign in to Openbase on this computer first."
        return entry
    try:
        response = httpx.get(
            f"{peer.base_url}{SETTINGS_PATH}",
            headers=_auth_headers(token),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        entry["error"] = "Offline or Openbase is not running."
        return entry
    entry["reachable"] = True
    if response.status_code in (401, 403):
        entry["error"] = "Signed in to a different Openbase account."
        return entry
    if response.status_code != 200:
        entry["error"] = f"Unexpected answer (HTTP {response.status_code})."
        return entry
    try:
        data = response.json()
    except ValueError:
        entry["error"] = "Unexpected answer."
        return entry
    if not isinstance(data, dict):
        entry["error"] = "Unexpected answer."
        return entry
    entry["role"] = _role(data)
    if entry["role"] == "edge":
        entry["hub_host"] = split_host_port(str(data.get("peer_hot") or ""))[0] or None
    return entry


def _probe_all(peers: list[fleet.FleetPeer], token: str | None) -> list[dict[str, Any]]:
    if not peers:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
        return list(pool.map(lambda peer: _probe(peer, token), peers))


def _peer_name_for_host(
    host: str, peers: list[fleet.FleetPeer], self_hosts: set[str]
) -> str | None:
    if not host:
        return None
    if host.lower() in self_hosts:
        return "this computer"
    for peer in peers:
        if _peer_matches(peer, host):
            return peer.name
    return None


def candidates() -> dict[str, Any]:
    """The user's other computers on Openbase VPN, with their sync roles."""
    token = fleet.owner_access_token()
    peers = _pairing_peers()
    entries = _probe_all(peers, token)
    self_hosts = _self_hosts()
    for entry in entries:
        if entry["hub_host"]:
            entry["hub_name"] = _peer_name_for_host(
                entry["hub_host"], peers, self_hosts
            )
    summary = _summary()
    return {
        "signed_in": token is not None,
        "role": _role(summary),
        "candidates": entries,
    }


def hub_display(summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Which computer is the hub, for the configured view (never raises)."""
    summary = summary if summary is not None else _summary()
    role = _role(summary)
    if role == "hub":
        return {"hub_is_self": True, "hub_host": None, "hub_name": None}
    if role != "edge":
        return {}
    host = split_host_port(str(summary.get("peer_hot") or ""))[0]
    name = None
    try:
        peer = find_pairing_peer(host) if host else None
        name = peer.name if peer else None
    except Exception:  # noqa: BLE001 - a display lookup must never break settings
        name = None
    return {"hub_is_self": False, "hub_host": host or None, "hub_name": name}


# --- hub -------------------------------------------------------------------


def become_hub(roots: list[str] | None = None, group: str = "default") -> dict:
    """Make this computer the hub and start the sync service."""
    from openbase_coder_cli.services.network import tailscale_ip

    with _lock:
        _require_unconfigured()
        _require_token()
        _require_daemon_binary()
        ip = tailscale_ip("4")
        if not ip:
            raise PairingError(
                "no_vpn",
                "This computer is not connected to Openbase VPN. Connect it "
                "and try again.",
                409,
            )
        for root in roots or []:
            check_root_allowed(root)
        entries, change = sync_daemon.plan_root_additions([], roots or default_roots())
        if not entries:
            raise PairingError("no_roots", "Choose at least one folder to sync.", 400)
        _ensure_root_dirs(entries)
        config = sync_daemon.SyncDaemonConfig(
            device_id=sync_daemon.default_device_id(),
            sync_group=group,
            role="hub",
            pair_secret=sync_daemon.new_pair_secret(),
            roots=entries,
            listen_hot=_address(ip, sync_daemon.DEFAULT_HOT_PORT),
            listen_bulk=_address(ip, sync_daemon.DEFAULT_BULK_PORT),
            anchor=DEFAULT_ANCHOR,
        )
        sync_daemon.write_config(config)
        _start_service()
    return {
        "role": "hub",
        "roots": entries,
        "skipped": [{"path": p, "reason": r} for p, r in change.skipped],
    }


def offer() -> dict[str, Any]:
    """What an edge needs to pair with this hub, including the pair secret.

    Only answered by a computer already set up as the hub.
    """
    path = sync_daemon.SYNC_DAEMON_CONFIG_PATH
    if not path.is_file():
        raise PairingError(
            "hub_not_configured", "This computer is not set up as a hub.", 409
        )
    try:
        data = sync_daemon._load_config(path)
    except Exception as exc:  # noqa: BLE001
        raise PairingError(
            "hub_config_unreadable",
            "The sync setup on this computer is unreadable.",
            500,
        ) from exc
    if data.get("role") != "hub":
        raise PairingError(
            "hub_not_configured",
            "This computer is not the hub; it syncs with another computer.",
            409,
        )
    hot_port = split_host_port(str(data.get("listen_hot") or ""))[1]
    bulk_port = split_host_port(str(data.get("listen_bulk") or ""))[1]
    secret = data.get("pair_secret")
    if not hot_port or not bulk_port or not isinstance(secret, str) or not secret:
        raise PairingError(
            "hub_config_unreadable",
            "The sync setup on this computer is incomplete.",
            500,
        )
    placement = data.get("placement") if isinstance(data.get("placement"), dict) else {}
    anchor = (
        placement.get("anchor") if placement.get("anchor") in {"hub", "edge"} else "hub"
    )
    return {
        "pair_secret": secret,
        "hot_port": hot_port,
        "bulk_port": bulk_port,
        "sync_group": str(data.get("sync_group") or "default"),
        "anchor": anchor,
        "roots": sync_daemon._roots_from_config(data),
    }


# --- edge ------------------------------------------------------------------


def _request_offer(peer: fleet.FleetPeer, token: str) -> dict[str, Any]:
    try:
        response = httpx.post(
            f"{peer.base_url}{OFFER_PATH}",
            headers=_auth_headers(token),
            json={},
            timeout=PAIR_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise PairingError(
            "hub_unreachable",
            f"Could not reach {peer.name}. Make sure it is on and connected "
            "to Openbase VPN.",
            502,
        ) from exc
    if response.status_code in (401, 403):
        raise PairingError(
            "other_account",
            f"{peer.name} is not signed in to your Openbase account. Sign both "
            "computers in to the same account.",
            409,
        )
    if response.status_code == 404:
        raise PairingError(
            "hub_outdated",
            f"{peer.name} runs an older Openbase. Update Openbase on it and try again.",
            409,
        )
    if response.status_code == 409:
        raise PairingError(
            "hub_not_configured",
            f"{peer.name} is not set up as the always-on computer yet. On "
            f'{peer.name}, open Sync and choose "Make this my always-on '
            'computer".',
            409,
        )
    if response.status_code != 200:
        raise PairingError(
            "hub_error",
            f"{peer.name} could not pair (HTTP {response.status_code}).",
            502,
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    valid = (
        isinstance(payload, dict)
        and isinstance(payload.get("pair_secret"), str)
        and payload["pair_secret"]
        and isinstance(payload.get("hot_port"), int)
        and isinstance(payload.get("bulk_port"), int)
        and isinstance(payload.get("roots"), list)
    )
    if not valid:
        raise PairingError("hub_error", f"{peer.name} sent an invalid answer.", 502)
    return payload


def _offer_roots(payload: dict[str, Any]) -> list[dict[str, Any]]:
    roots: list[dict[str, Any]] = []
    for raw in payload.get("roots") or []:
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("path"):
            continue
        root: dict[str, Any] = {"id": str(raw["id"]), "path": str(raw["path"])}
        pins = raw.get("pins")
        if isinstance(pins, list) and pins:
            root["pins"] = [str(pin) for pin in pins]
        roots.append(root)
    return roots


def _select_roots(
    hub_roots: list[dict[str, Any]], wanted: list[str] | None, hub_name: str
) -> list[dict[str, Any]]:
    if not wanted:
        return hub_roots
    selected: list[dict[str, Any]] = []
    for path in wanted:
        target = sync_daemon.expand_root_path(path)
        match = next(
            (
                root
                for root in hub_roots
                if sync_daemon.expand_root_path(root["path"]) == target
            ),
            None,
        )
        if match is None:
            raise PairingError(
                "root_not_on_hub",
                f"{hub_name} does not sync {path}. Add it there first.",
                400,
            )
        if match not in selected:
            selected.append(match)
    return selected


def join_hub(hub: str, roots: list[str] | None = None) -> dict[str, Any]:
    """Make this computer an edge of ``hub`` (a peer id, name or host)."""
    with _lock:
        _require_unconfigured()
        _require_daemon_binary()
        token = _require_token()
        peer = find_pairing_peer(hub)
        if peer is None:
            raise PairingError(
                "hub_not_found",
                f"{hub} is not one of your computers on Openbase VPN, or it is "
                "offline.",
                404,
            )
        payload = _request_offer(peer, token)
        hub_roots = _offer_roots(payload)
        if not hub_roots:
            raise PairingError(
                "hub_error", f"{peer.name} does not sync any folders yet.", 409
            )
        selected = _select_roots(hub_roots, roots, peer.name)
        for root in selected:
            check_root_allowed(root["path"])
        _ensure_root_dirs(selected)
        anchor = payload.get("anchor")
        config = sync_daemon.SyncDaemonConfig(
            device_id=sync_daemon.default_device_id(),
            sync_group=str(payload.get("sync_group") or "default"),
            role="edge",
            pair_secret=payload["pair_secret"],
            roots=selected,
            peer_hot=_address(peer.key, payload["hot_port"]),
            peer_bulk=_address(peer.key, payload["bulk_port"]),
            anchor=anchor if anchor in {"hub", "edge"} else DEFAULT_ANCHOR,
        )
        sync_daemon.write_config(config)
        _start_service()
    return {
        "role": "edge",
        "hub_name": peer.name,
        "hub_host": peer.key,
        "roots": selected,
    }


# --- leave -----------------------------------------------------------------


def trash_dir() -> Path:
    return OPENBASE_BASE_DIR / "trash"


def leave() -> dict[str, Any]:
    """Stop syncing on this computer: stop the service, trash the config."""
    with _lock:
        config_path = sync_daemon.SYNC_DAEMON_CONFIG_PATH
        if not config_path.is_file():
            return {"left": False, "config_moved_to": None}
        _stop_service()
        trash = trash_dir()
        trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = trash / f"sync-config-{stamp}.toml"
        counter = 1
        while destination.exists():
            destination = trash / f"sync-config-{stamp}-{counter}.toml"
            counter += 1
        shutil.move(str(config_path), str(destination))
    return {"left": True, "config_moved_to": str(destination)}


# --- roots -----------------------------------------------------------------


def list_roots() -> dict[str, Any]:
    summary = _summary()
    return {
        "configured": bool(summary.get("configured")),
        "role": _role(summary),
        "roots": summary.get("roots") or [],
    }


def _require_configured_role() -> str:
    summary = _summary()
    role = _role(summary)
    if role == "none":
        raise PairingError(
            "not_configured", "Openbase Sync is not set up on this computer.", 409
        )
    return role


def _hub_peer_from_config() -> fleet.FleetPeer:
    host = split_host_port(str(_summary().get("peer_hot") or ""))[0]
    if not host:
        raise PairingError("hub_unknown", "The hub address is missing.", 409)
    peer = find_pairing_peer(host)
    if peer is not None:
        return peer
    from openbase_coder_cli.services.tailnet_devices import (
        OPENBASE_CODER_TAILNET_PORT,
    )

    literal = f"[{host}]" if ":" in host else host
    return fleet.FleetPeer(
        key=host,
        name=host,
        base_url=f"http://{literal}:{OPENBASE_CODER_TAILNET_PORT}",
    )


def _edge_peers() -> list[fleet.FleetPeer]:
    """The online computers that sync as edges of this hub."""
    token = fleet.owner_access_token()
    if not token:
        return []
    peers = _pairing_peers()
    self_hosts = _self_hosts()
    edges: list[fleet.FleetPeer] = []
    for peer, entry in zip(peers, _probe_all(peers, token), strict=False):
        if entry["role"] == "edge" and (entry["hub_host"] or "").lower() in self_hosts:
            edges.append(peer)
    return edges


def _apply_on_peer(peer: fleet.FleetPeer, token: str, action: str, path: str) -> None:
    """Make the same root change on a paired computer (raises PairingError)."""
    method = "POST" if action == "add" else "DELETE"
    try:
        response = httpx.request(
            method,
            f"{peer.base_url}{ROOTS_PATH}",
            headers=_auth_headers(token),
            json={"path": path, "local_only": True},
            timeout=PAIR_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise PairingError(
            "peer_unreachable",
            f"Could not reach {peer.name}. Make sure it is on and connected "
            "to Openbase VPN.",
            502,
        ) from exc
    if response.status_code in (401, 403):
        raise PairingError(
            "other_account",
            f"{peer.name} is not signed in to your Openbase account.",
            409,
        )
    if response.status_code == 404 and action == "add":
        raise PairingError(
            "peer_outdated",
            f"{peer.name} runs an older Openbase. Update Openbase on it and try again.",
            409,
        )
    if response.status_code >= 400 and response.status_code != 404:
        try:
            message = (response.json() or {}).get("error")
        except ValueError:
            message = None
        raise PairingError(
            "peer_error",
            f"{peer.name}: {message or f'HTTP {response.status_code}'}",
            502,
        )


def _propagate(action: str, role: str, path: str) -> list[dict[str, Any]]:
    """Apply a root change on the other side(s).

    An edge changes its hub first and fails if it cannot, so the two never
    disagree. A hub applies its own change and then updates the edges it
    can reach, reporting the rest.
    """
    if role == "edge":
        token = _require_token()
        peer = _hub_peer_from_config()
        _apply_on_peer(peer, token, action, path)
        return [{"name": peer.name, "ok": True, "error": None}]
    results: list[dict[str, Any]] = []
    token = fleet.owner_access_token()
    if not token:
        return results
    for peer in _edge_peers():
        try:
            _apply_on_peer(peer, token, action, path)
            results.append({"name": peer.name, "ok": True, "error": None})
        except PairingError as exc:
            results.append({"name": peer.name, "ok": False, "error": str(exc)})
    return results


def add_root(path: str, *, local_only: bool = False) -> dict[str, Any]:
    """Sync another folder, here and on the paired computer(s).

    ``local_only`` is the request a paired computer sends: create the folder
    if needed, treat an existing root as success and do not propagate.
    """
    if not path or not str(path).strip():
        raise PairingError("path_required", "Choose a folder.", 400)
    check_root_allowed(path)
    with _lock:
        role = _require_configured_role()
        target = sync_daemon.expand_root_path(path)
        if local_only:
            target.mkdir(parents=True, exist_ok=True)
        elif not target.is_dir():
            raise PairingError(
                "folder_missing", f"{path} is not a folder on this computer.", 400
            )
        roots, change = sync_daemon.plan_root_additions(
            sync_daemon.configured_roots(), [path]
        )
        if not change.changed:
            reason = change.skipped[0][1] if change.skipped else "already synced"
            if local_only and reason.startswith("already inside"):
                return {"root": None, "roots": roots, "restarted": False, "peers": []}
            raise PairingError("root_overlap", f"Not added: {reason}.", 409)
        if change.replaced:
            inner = ", ".join(root["path"] for root in change.replaced)
            raise PairingError(
                "root_overlap", f"Not added: it contains synced folder(s) {inner}.", 409
            )
        entry = change.added[0]
        peers: list[dict[str, Any]] = []
        if not local_only and role == "edge":
            peers = _propagate("add", role, entry["path"])
        sync_daemon.set_roots(roots)
        restarted = sync_daemon.restart_service_if_installed()
        if not local_only and role == "hub":
            peers = _propagate("add", role, entry["path"])
    return {"root": entry, "roots": roots, "restarted": restarted, "peers": peers}


def remove_root(path: str, *, local_only: bool = False) -> dict[str, Any]:
    """Stop syncing a folder, here and on the paired computer(s). Files stay."""
    if not path or not str(path).strip():
        raise PairingError("path_required", "Choose a folder.", 400)
    with _lock:
        role = _require_configured_role()
        target = sync_daemon.expand_root_path(path)
        current = sync_daemon.configured_roots()
        match = [
            root
            for root in current
            if root.get("path") and sync_daemon.expand_root_path(root["path"]) == target
        ]
        if not match:
            if local_only:
                return {
                    "removed": [],
                    "roots": current,
                    "restarted": False,
                    "peers": [],
                }
            raise PairingError("root_not_found", f"{path} is not synced.", 404)
        if len(current) == len(match):
            raise PairingError(
                "last_root",
                "Keep at least one folder. To stop syncing, use Stop syncing "
                "on this computer.",
                409,
            )
        home_relative = match[0]["path"]
        peers: list[dict[str, Any]] = []
        if not local_only and role == "edge":
            peers = _propagate("remove", role, home_relative)
        removed = sync_daemon.remove_roots([path])
        restarted = sync_daemon.restart_service_if_installed()
        if not local_only and role == "hub":
            peers = _propagate("remove", role, home_relative)
    return {
        "removed": removed,
        "roots": sync_daemon.configured_roots(),
        "restarted": restarted,
        "peers": peers,
    }
