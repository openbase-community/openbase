"""Pair computers for Openbase Sync, shared by the console and the CLI.

The console Sync page and ``openbase-coder sync-daemon pair`` both call these
functions, so there is one code path:

- ``candidates()`` lists the user's other computers on Openbase VPN with
  their sync role.
- ``become_hub()`` makes this computer the hub (the always-on computer).
- ``hub_folders(hub)`` previews the hub's folders before joining, with the
  files and bytes each holds (from the hub's daemon) and this computer's
  free disk, so the user can choose which to sync here.
- ``join_hub(hub, roots)`` makes this computer an edge of that hub, syncing
  all of the hub's folders or the chosen subset. The hub hands over its pair
  secret, ports and roots through ``offer()``. A cloud workspace joins
  project-only: no folder is preselected, and large files stay placeholders
  there until used.
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

from openbase_coder_cli.services.cloud_workspace import is_cloud_workspace
from openbase_coder_cli import sync_daemon
from openbase_coder_cli.paths import OPENBASE_BASE_DIR
from openbase_coder_cli.services import fleet_aggregation as fleet

logger = logging.getLogger(__name__)

SETTINGS_PATH = "/api/sync/daemon/settings/"
OFFER_PATH = "/api/sync/daemon/pairing/offer/"
FOLDERS_PATH = "/api/sync/daemon/pairing/folders/"
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


def disk_usage(path: str | Path = "~") -> dict[str, int | None]:
    """Free and total bytes of the volume holding ``path`` (None unknown)."""
    try:
        usage = shutil.disk_usage(sync_daemon.expand_root_path(path))
    except OSError:
        return {"free_bytes": None, "total_bytes": None}
    return {"free_bytes": int(usage.free), "total_bytes": int(usage.total)}


def this_computer() -> dict[str, Any]:
    """What a join preview needs to know about this computer."""
    cloud = is_cloud_workspace()
    return {
        "cloud_workspace": cloud,
        # a cloud workspace has a small disk and works on a project or two
        "project_only_default": cloud,
        "disk": disk_usage(),
    }


# What the installed sync engine supports, from ``openbase-syncd --features``
# (one name per line). Engines before project-only support reject the flag.
PROJECT_ONLY_FEATURE = "project-only"


def engine_features() -> set[str]:
    """Features of the installed ``openbase-syncd`` (empty when unknown)."""
    import subprocess

    try:
        from openbase_coder_cli.services.installation import InstallationConfig
        from openbase_coder_cli.services.launchd import _binary_resolvers

        binary = _binary_resolvers(InstallationConfig.load())["openbase_syncd"]()
    except Exception:  # noqa: BLE001 - any failure means "unknown"
        return set()
    if not binary:
        return set()
    try:
        result = subprocess.run(
            [binary, "--features"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _require_project_only_engine() -> None:
    """Refuse a project-only setup the installed engine would not honor: an
    older engine ignores ``only`` and ``thin`` and keeps a fixed 10 GB free,
    so a small cloud workspace would try to take every file of the folder."""
    if PROJECT_ONLY_FEATURE not in engine_features():
        raise PairingError(
            "engine_outdated",
            "Syncing only some projects needs a newer Openbase Sync on this "
            "computer. Update Openbase and try again.",
            409,
        )


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
        base = sync_daemon.expand_root_path(root["path"])
        base.mkdir(parents=True, exist_ok=True)
        for rel in root.get("only") or []:
            (base / rel).mkdir(parents=True, exist_ok=True)


def _service():
    from openbase_coder_cli.services.registry import find_service

    return find_service(sync_daemon.SYNC_DAEMON_SERVICE_NAME)


def _externally_supervised() -> bool:
    """Whether something other than launchd/systemd runs the services.

    The Docker image supervises services itself and only starts the sync
    daemon at container start, so changes there take effect on a restart.
    """
    from openbase_coder_cli.services.launchd import _external_supervisor

    return _external_supervisor()


def _start_service() -> bool:
    """Install and start the service; True when a restart is still needed."""
    from openbase_coder_cli.services.installation import InstallationConfig
    from openbase_coder_cli.services.launchd import (
        install_service,
        regenerate_service,
    )

    try:
        if _externally_supervised():
            regenerate_service(InstallationConfig.load(), _service())
            return True
        install_service(InstallationConfig.load(), _service())
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear message
        raise PairingError(
            "service_failed",
            f"Saved the sync setup, but the sync service did not start ({exc}). "
            "Start it with 'openbase-coder services start sync-daemon'.",
            500,
        ) from exc
    return False


def _stop_service() -> bool:
    """Stop and remove the service; True when a restart is still needed."""
    from openbase_coder_cli.services.launchd import _wrapper_path, remove_service

    try:
        if _externally_supervised():
            _wrapper_path(_service()).unlink(missing_ok=True)
            return True
        remove_service(_service())
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear message
        raise PairingError(
            "service_failed",
            f"Could not stop the sync service ({exc}). Nothing was changed.",
            500,
        ) from exc
    return False


def _restart_service() -> tuple[bool, bool]:
    """Restart after a root change: (restarted, restart_required)."""
    if _externally_supervised():
        return False, True
    return sync_daemon.restart_service_if_installed(), False


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
        restart_required = _start_service()
    return {
        "role": "hub",
        "roots": entries,
        "skipped": [{"path": p, "reason": r} for p, r in change.skipped],
        "restart_required": restart_required,
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
        # ignores keep paths local to one computer: never hand them on
        "roots": _with_estimates(
            [
                {
                    key: value
                    for key, value in root.items()
                    if key not in ("ignore", "only")
                }
                for root in sync_daemon._roots_from_config(data)
            ]
        ),
    }


def _root_estimates() -> dict[str, dict[str, int]]:
    """Files and bytes per root id from this computer's daemon (best effort)."""
    try:
        status = sync_daemon.SyncDaemonClient(timeout=2.0).status()
    except sync_daemon.SyncDaemonError:
        return {}
    out: dict[str, dict[str, int]] = {}
    for root in status.get("roots") or []:
        if not isinstance(root, dict) or not root.get("id"):
            continue
        estimate = {"files": int(root.get("entries") or 0)}
        if "bytes" in root:  # older daemons report no sizes
            estimate["bytes"] = int(root.get("bytes") or 0)
        out[str(root["id"])] = estimate
    return out


SUBFOLDER_TIMEOUT_SECONDS = 30.0


def _subfolders(root: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The folders directly inside a root (the projects of ~/Projects) with
    their files and bytes, from this computer's daemon. None when the daemon
    cannot say (not running, or too old to know ``folder-sizes``)."""
    try:
        sizes = sync_daemon.SyncDaemonClient(
            timeout=SUBFOLDER_TIMEOUT_SECONDS
        ).folder_sizes(str(root["id"]))
    except sync_daemon.SyncDaemonError:
        return None
    base = str(root["path"]).rstrip("/")
    return [
        {
            "name": str(item.get("name")),
            "path": f"{base}/{item.get('name')}",
            "files": int(item.get("files") or 0),
            "bytes": int(item.get("bytes") or 0),
        }
        for item in sizes
        if item.get("dir") and item.get("name")
    ]


def _with_estimates(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    estimates = _root_estimates()
    out = []
    for root in roots:
        estimate = estimates.get(str(root.get("id") or ""), {})
        out.append(
            {
                **root,
                "files": estimate.get("files"),
                "bytes": estimate.get("bytes"),
            }
        )
    return out


def folders() -> dict[str, Any]:
    """This hub's folders with their size, for a join preview.

    Answered by the hub only. Unlike ``offer()`` it carries no secret, so a
    computer can show the choice before it commits to pairing.
    """
    path = sync_daemon.SYNC_DAEMON_CONFIG_PATH
    summary = _summary()
    if not path.is_file() or summary.get("role") != "hub":
        raise PairingError(
            "hub_not_configured", "This computer is not set up as a hub.", 409
        )
    roots = [
        {key: value for key, value in root.items() if key not in ("ignore", "only")}
        for root in summary.get("roots") or []
    ]
    roots = _with_estimates(roots)
    for root in roots:
        root["subfolders"] = _subfolders(root)
    return {"roots": roots, "disk": disk_usage()}


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


def _containing_root(
    roots: list[dict[str, Any]], path: str
) -> tuple[dict[str, Any] | None, str | None]:
    """The root that is ``path`` (rel None) or contains it (rel: root-relative)."""
    target = sync_daemon.expand_root_path(path)
    for root in roots:
        if not root.get("path"):
            continue
        base = sync_daemon.expand_root_path(root["path"])
        if target == base:
            return root, None
        if base in target.parents:
            return root, target.relative_to(base).as_posix()
    return None, None


def _select_roots(
    hub_roots: list[dict[str, Any]], wanted: list[str] | None, hub_name: str
) -> list[dict[str, Any]]:
    """The hub's roots this computer syncs. A wanted path is one of the
    hub's folders (all of it) or a folder inside one (only that part: a
    project of ~/Projects)."""
    if not wanted:
        return hub_roots
    whole: set[str] = set()
    parts: dict[str, list[str]] = {}
    for path in wanted:
        root, rel = _containing_root(hub_roots, path)
        if root is None:
            raise PairingError(
                "root_not_on_hub",
                f"{hub_name} does not sync {path}. Add it there first.",
                400,
            )
        if rel is None:
            whole.add(root["id"])
        elif rel not in parts.setdefault(root["id"], []):
            parts[root["id"]].append(rel)
    selected: list[dict[str, Any]] = []
    for root in hub_roots:
        if root["id"] in whole:
            selected.append(root)
        elif root["id"] in parts:
            selected.append({**root, "only": sorted(parts[root["id"]])})
    return selected


def _find_hub(hub: str) -> fleet.FleetPeer:
    peer = find_pairing_peer(hub)
    if peer is None:
        raise PairingError(
            "hub_not_found",
            f"{hub} is not one of your computers on Openbase VPN, or it is offline.",
            404,
        )
    return peer


def _request_folders(peer: fleet.FleetPeer, token: str) -> list[dict[str, Any]]:
    """The hub's folders with sizes; an older hub's offer when it has no preview."""
    try:
        response = httpx.get(
            f"{peer.base_url}{FOLDERS_PATH}",
            headers=_auth_headers(token),
            timeout=PAIR_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise PairingError(
            "hub_unreachable",
            f"Could not reach {peer.name}. Make sure it is on and connected "
            "to Openbase VPN.",
            502,
        ) from exc
    if response.status_code == 404:
        payload = _request_offer(peer, token)
        return [
            {**root, "files": None, "bytes": None, "subfolders": None}
            for root in _offer_roots(payload)
        ]
    if response.status_code in (401, 403):
        raise PairingError(
            "other_account",
            f"{peer.name} is not signed in to your Openbase account.",
            409,
        )
    if response.status_code == 409:
        raise PairingError(
            "hub_not_configured",
            f"{peer.name} is not set up as the always-on computer yet.",
            409,
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code != 200 or not isinstance(payload, dict):
        raise PairingError(
            "hub_error",
            f"{peer.name} could not list its folders (HTTP {response.status_code}).",
            502,
        )
    out: list[dict[str, Any]] = []
    for raw in payload.get("roots") or []:
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("path"):
            continue
        out.append(
            {
                "id": str(raw["id"]),
                "path": str(raw["path"]),
                "files": raw.get("files")
                if isinstance(raw.get("files"), int)
                else None,
                "bytes": raw.get("bytes")
                if isinstance(raw.get("bytes"), int)
                else None,
                "subfolders": _parse_subfolders(raw.get("subfolders")),
            }
        )
    return out


def _parse_subfolders(raw: Any) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        return None
    out = []
    for sub in raw:
        if not isinstance(sub, dict) or not sub.get("name") or not sub.get("path"):
            continue
        out.append(
            {
                "name": str(sub["name"]),
                "path": str(sub["path"]),
                "files": sub.get("files")
                if isinstance(sub.get("files"), int)
                else None,
                "bytes": sub.get("bytes")
                if isinstance(sub.get("bytes"), int)
                else None,
            }
        )
    return out


def hub_folders(hub: str) -> dict[str, Any]:
    """Preview of joining ``hub``: its folders, their sizes, this computer.

    ``selected`` is the suggested choice: every folder for a laptop or
    desktop, none for a cloud workspace (project-only: the user picks).
    """
    token = _require_token()
    peer = _find_hub(hub)
    local = this_computer()
    project_only = local["project_only_default"]
    roots = _request_folders(peer, token)
    synced = {
        sync_daemon.expand_root_path(root["path"])
        for root in sync_daemon.configured_roots()
        if root.get("path")
    }
    folders_out = []
    for root in roots:
        target = sync_daemon.expand_root_path(root["path"])
        subfolders = root.get("subfolders")
        folders_out.append(
            {
                **root,
                "subfolders": (
                    [{**sub, "selected": False} for sub in subfolders]
                    if subfolders is not None
                    else None
                ),
                "synced_here": target in synced,
                "exists_here": target.is_dir(),
                "selected": not project_only,
            }
        )
    return {
        "hub_name": peer.name,
        "hub_host": peer.key,
        "folders": folders_out,
        "this_computer": local,
        "project_only": project_only,
    }


def _disk_warning(
    selected: list[dict[str, Any]], estimates: dict[str, Any]
) -> str | None:
    """A warning when the chosen folders look bigger than this disk's room."""
    total = 0
    for root in selected:
        size = estimates.get(root["id"])
        if isinstance(size, int):
            total += size
    free = disk_usage().get("free_bytes")
    if not total or free is None or total < free * 0.9:
        return None
    return (
        f"The chosen folders hold about {_human_bytes(total)} and this "
        f"computer has {_human_bytes(free)} free. Large files stay on the hub "
        "until used, but consider syncing fewer folders."
    )


def _human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{n} B"


def join_hub(
    hub: str,
    roots: list[str] | None = None,
    *,
    project_only: bool | None = None,
) -> dict[str, Any]:
    """Make this computer an edge of ``hub`` (a peer id, name or host).

    ``roots`` chooses which of the hub's folders sync here (default: all).
    ``project_only`` (default: whether this is a cloud workspace) requires an
    explicit choice of folders and keeps large files as placeholders here.
    """
    if project_only is None:
        project_only = is_cloud_workspace()
    with _lock:
        _require_unconfigured()
        _require_daemon_binary()
        token = _require_token()
        peer = _find_hub(hub)
        payload = _request_offer(peer, token)
        hub_roots = _offer_roots(payload)
        if not hub_roots:
            raise PairingError(
                "hub_error", f"{peer.name} does not sync any folders yet.", 409
            )
        if project_only and not roots:
            names = ", ".join(root["path"] for root in hub_roots)
            raise PairingError(
                "choose_folders",
                "This computer syncs only the projects you choose. Pick one or "
                f"more of {peer.name}'s folders: {names}.",
                400,
            )
        selected = _select_roots(hub_roots, roots, peer.name)
        if project_only or any(root.get("only") for root in selected):
            _require_project_only_engine()
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
            thin=True if project_only else None,
        )
        sizes = {
            str(raw.get("id")): raw.get("bytes")
            for raw in payload.get("roots") or []
            if isinstance(raw, dict)
        }
        # a root synced in part: its full size overstates; leave it out
        warning = _disk_warning([r for r in selected if not r.get("only")], sizes)
        sync_daemon.write_config(config)
        restart_required = _start_service()
    return {
        "role": "edge",
        "hub_name": peer.name,
        "hub_host": peer.key,
        "roots": selected,
        "project_only": project_only,
        "warnings": [warning] if warning else [],
        "restart_required": restart_required,
    }


# --- leave -----------------------------------------------------------------


def trash_dir() -> Path:
    return OPENBASE_BASE_DIR / "trash"


def leave() -> dict[str, Any]:
    """Stop syncing on this computer: stop the service, trash the config."""
    with _lock:
        config_path = sync_daemon.SYNC_DAEMON_CONFIG_PATH
        if not config_path.is_file():
            return {"left": False, "config_moved_to": None, "restart_required": False}
        restart_required = _stop_service()
        trash = trash_dir()
        trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = trash / f"sync-config-{stamp}.toml"
        counter = 1
        while destination.exists():
            destination = trash / f"sync-config-{stamp}-{counter}.toml"
            counter += 1
        shutil.move(str(config_path), str(destination))
    return {
        "left": True,
        "config_moved_to": str(destination),
        "restart_required": restart_required,
    }


# --- roots -----------------------------------------------------------------


def list_roots() -> dict[str, Any]:
    summary = _summary()
    return {
        "configured": bool(summary.get("configured")),
        "role": _role(summary),
        "project_only": bool(summary.get("project_only")),
        "roots": summary.get("roots") or [],
    }


def available_roots() -> dict[str, Any]:
    """For an edge: the hub's folders, which sync here, and their sizes.

    The Sync page uses it to add one of the hub's folders to a computer that
    syncs only some of them, or to stop syncing one here only.
    """
    role = _require_configured_role()
    if role != "edge":
        return {"role": role, "hub_name": None, "folders": [], "disk": disk_usage()}
    token = _require_token()
    peer = _hub_peer_from_config()
    local = {
        sync_daemon.expand_root_path(root["path"]): root
        for root in sync_daemon.configured_roots()
        if root.get("path")
    }
    folders_out = []
    for root in _request_folders(peer, token):
        mine = local.get(sync_daemon.expand_root_path(root["path"]))
        only = list(mine.get("only") or []) if mine else []
        whole = mine is not None and not only
        subfolders = root.get("subfolders")
        folders_out.append(
            {
                **root,
                "synced_here": whole,
                "partly_synced_here": bool(only),
                "only": only,
                "subfolders": (
                    [
                        {**sub, "synced_here": whole or sub["name"] in only}
                        for sub in subfolders
                    ]
                    if subfolders is not None
                    else None
                ),
            }
        )
    return {
        "role": role,
        "hub_name": peer.name,
        "project_only": bool(_summary().get("project_only")),
        "folders": folders_out,
        "disk": disk_usage(),
    }


def _hub_roots_or_none() -> list[dict[str, Any]] | None:
    try:
        return _request_folders(_hub_peer_from_config(), _require_token())
    except PairingError:
        return None


def _add_part(path: str) -> dict[str, Any] | None:
    """On an edge: sync ``path``, a folder inside one of the hub's folders
    (a project of ~/Projects), on this computer only. None when ``path`` is
    not such a folder (the caller handles it as a whole folder)."""
    entry, roots = _plan_part(path)
    if entry is None:
        return None
    _require_project_only_engine()
    sync_daemon.expand_root_path(path).mkdir(parents=True, exist_ok=True)
    sync_daemon.set_roots(roots)
    restarted, restart_required = _restart_service()
    return {
        "root": entry,
        "roots": roots,
        "restarted": restarted,
        "restart_required": restart_required,
        "peers": [],
    }


def _plan_part(path: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """(the root entry, the new root list) for adding ``path`` as a project
    of a folder; (None, []) when it is not one."""
    current = sync_daemon.configured_roots()
    root, rel = _containing_root(current, path)
    if root is not None:
        if rel is None or not root.get("only"):
            return None, []  # the folder itself, or inside a folder synced whole
        only = list(root["only"])
        if any(rel == item or rel.startswith(item + "/") for item in only):
            raise PairingError(
                "root_overlap", f"Not added: {path} already syncs here.", 409
            )
        only = sorted([item for item in only if not item.startswith(rel + "/")] + [rel])
        roots = [{**r, "only": only} if r is root else r for r in current]
        entry = {**root, "only": only}
    else:
        partial = bool(_summary().get("project_only")) or any(
            r.get("only") for r in current
        )
        if not partial:
            return None, []  # a full copy adds whole folders only
        hub_roots = _hub_roots_or_none()
        hub_root, rel = _containing_root(hub_roots or [], path)
        if hub_root is None or rel is None:
            return None, []
        entry = {"id": hub_root["id"], "path": hub_root["path"], "only": [rel]}
        overlap = [
            r["path"]
            for r in current
            if r.get("path") and sync_daemon.roots_overlap(r["path"], hub_root["path"])
        ]
        if overlap:
            raise PairingError(
                "root_overlap",
                f"Not added: {hub_root['path']} overlaps synced folder(s) "
                f"{', '.join(overlap)}.",
                409,
            )
        roots = [*current, entry]
    return entry, roots


def _remove_part(path: str, scope: str | None) -> dict[str, Any] | None:
    """On an edge: stop syncing ``path`` when it is one of a folder's only
    paths. None when it is not (the caller handles a whole folder)."""
    current = sync_daemon.configured_roots()
    root, rel = _containing_root(current, path)
    if root is None or rel is None or rel not in (root.get("only") or []):
        return None
    if scope == "everywhere":
        raise PairingError(
            "part_of_folder",
            f"{path} is part of {root['path']}, which your other computers sync "
            "whole. It can stop syncing on this computer only.",
            409,
        )
    only = [item for item in root["only"] if item != rel]
    if only:
        roots = [{**r, "only": only} if r is root else r for r in current]
    elif len(current) == 1:
        raise PairingError(
            "last_root",
            "Keep at least one folder. To stop syncing, use Stop syncing "
            "on this computer.",
            409,
        )
    else:
        roots = [r for r in current if r is not root]
    sync_daemon.set_roots(roots)
    restarted, restart_required = _restart_service()
    return {
        "removed": [{"id": root["id"], "path": path}],
        "scope": "this_computer",
        "roots": roots,
        "restarted": restarted,
        "restart_required": restart_required,
        "peers": [],
    }


def _hub_has_root(path: str) -> bool:
    """Whether this edge's hub syncs ``path`` (False when it cannot be asked)."""
    try:
        token = _require_token()
        peer = _hub_peer_from_config()
        target = sync_daemon.expand_root_path(path)
        return any(
            sync_daemon.expand_root_path(root["path"]) == target
            for root in _request_folders(peer, token)
        )
    except PairingError:
        return False


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
        if role == "edge" and not local_only:
            part = _add_part(path)
            if part is not None:
                return part
        target = sync_daemon.expand_root_path(path)
        if local_only:
            target.mkdir(parents=True, exist_ok=True)
        elif not target.is_dir():
            # one of the hub's folders that this computer did not sync yet
            # (a project-only computer adding a project): it fills from the hub
            if role == "edge" and _hub_has_root(path):
                target.mkdir(parents=True, exist_ok=True)
            else:
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
        restarted, restart_required = _restart_service()
        if not local_only and role == "hub":
            peers = _propagate("add", role, entry["path"])
    return {
        "root": entry,
        "roots": roots,
        "restarted": restarted,
        "restart_required": restart_required,
        "peers": peers,
    }


def remove_root(
    path: str, *, local_only: bool = False, scope: str | None = None
) -> dict[str, Any]:
    """Stop syncing a folder, here and on the paired computer(s). Files stay.

    ``scope="this_computer"`` (an edge only) stops syncing it here and leaves
    the hub and the other computers syncing it; it is the default on a
    project-only computer. ``scope="everywhere"`` is the default elsewhere.
    """
    if not path or not str(path).strip():
        raise PairingError("path_required", "Choose a folder.", 400)
    if scope not in (None, "this_computer", "everywhere"):
        raise PairingError("bad_scope", "scope is this_computer or everywhere.", 400)
    peer_request = local_only  # a paired computer's request: missing is fine
    with _lock:
        role = _require_configured_role()
        requested_scope = scope
        if scope is None:
            project_only = bool(_summary().get("project_only"))
            scope = "this_computer" if role == "edge" and project_only else "everywhere"
        if role == "edge" and not peer_request:
            part = _remove_part(path, requested_scope)
            if part is not None:
                return part
        if scope == "this_computer":
            if role != "edge":
                raise PairingError(
                    "hub_holds_all",
                    "The always-on computer holds every synced folder. Stop "
                    "syncing it everywhere, or on the other computer only.",
                    409,
                )
            local_only = True
        target = sync_daemon.expand_root_path(path)
        current = sync_daemon.configured_roots()
        match = [
            root
            for root in current
            if root.get("path") and sync_daemon.expand_root_path(root["path"]) == target
        ]
        if not match:
            if peer_request:
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
        restarted, restart_required = _restart_service()
        if not local_only and role == "hub":
            peers = _propagate("remove", role, home_relative)
    return {
        "removed": removed,
        "scope": "this_computer" if local_only else "everywhere",
        "roots": sync_daemon.configured_roots(),
        "restarted": restarted,
        "restart_required": restart_required,
        "peers": peers,
    }
