"""Lifecycle operations for the code-sync subsystem.

Shared by the CLI (``openbase-coder sync ...``) and the settings API so both
surfaces enable/disable/apply identically: identity, rendered Syncthing
config, managed ignores, and the ``code-sync`` service.
"""

from __future__ import annotations

import socket
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import click

from openbase_coder_cli.code_sync import CodeSyncError
from openbase_coder_cli.code_sync.eligibility import (
    EligibilityResult,
    current_eligibility,
    syncable_peers,
)
from openbase_coder_cli.code_sync.ignores import update_all_ignores
from openbase_coder_cli.code_sync.syncthing import (
    SYNC_LISTEN_PORT,
    PeerDevice,
    SyncthingClient,
    ensure_identity,
    peer_address,
    write_config,
)
from openbase_coder_cli.paths import CODE_SYNC_DIR, SYNC_VERSIONS_DIR
from openbase_coder_cli.sync_config import (
    code_sync_enabled,
    set_code_sync_enabled,
)

CODE_SYNC_SERVICE_NAME = "code-sync"

# Config locations of a Syncthing instance the user manages themselves.
USER_SYNCTHING_CONFIG_PATHS = (
    Path("Library/Application Support/Syncthing/config.xml"),
    Path(".local/state/syncthing/config.xml"),
    Path(".config/syncthing/config.xml"),
)


def _managed_service_running() -> bool:
    from openbase_coder_cli.services.launchd import launchctl_status

    return bool(launchctl_status(_service_definition()).get("pid"))


def _user_managed_syncthing_running() -> bool:
    """A syncthing process that is not our managed instance."""
    result = subprocess.run(
        ["pgrep", "-fl", "syncthing"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        # "PID <argv...>": only count real syncthing executables, not
        # scripts whose command line merely mentions syncthing.
        parts = line.split(None, 2)
        if len(parts) < 2 or Path(parts[1]).name != "syncthing":
            continue
        if str(CODE_SYNC_DIR) not in line:
            return True
    return False


def _listen_port_available(port: int = SYNC_LISTEN_PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def user_managed_syncthing_folders(home: Path | None = None) -> list[Path]:
    """Folder paths configured in any user-managed Syncthing config."""
    home = home or Path.home()
    folders: list[Path] = []
    for rel in USER_SYNCTHING_CONFIG_PATHS:
        config_path = home / rel
        if not config_path.is_file():
            continue
        try:
            root = ET.parse(config_path).getroot()
        except (OSError, ET.ParseError):
            continue
        for element in root.findall("./folder"):
            raw = element.get("path") or ""
            if raw:
                folders.append(Path(raw).expanduser())
    return folders


def _paths_overlap(a: Path, b: Path) -> bool:
    try:
        a.relative_to(b)
        return True
    except ValueError:
        pass
    try:
        b.relative_to(a)
        return True
    except ValueError:
        return False


def ensure_port_available() -> None:
    """Refuse to arm sync when another Syncthing owns the listen port.

    Two instances cannot share tcp/22000; a user-managed Syncthing left
    running would leave the managed one unable to accept peer connections.
    """
    if _managed_service_running():
        return  # Re-render/restart of our own instance; the port is ours.
    if not _listen_port_available():
        raise CodeSyncError(
            f"Port {SYNC_LISTEN_PORT} is already in use"
            + (
                " by a user-managed Syncthing; stop it (or move its listen "
                "address to another port) before enabling code sync."
                if _user_managed_syncthing_running()
                else "; free it before enabling code sync."
            )
        )


def ensure_no_user_managed_overlap(folders, home: Path | None = None) -> None:
    """Refuse folders that a running user-managed Syncthing already syncs.

    Two sync engines over one directory echo each other's writes and
    manufacture conflict storms.
    """
    if not _user_managed_syncthing_running():
        return
    user_folders = user_managed_syncthing_folders(home)
    for folder in folders:
        managed_path = folder.absolute_path(home)
        for user_path in user_folders:
            if _paths_overlap(managed_path, user_path):
                raise CodeSyncError(
                    f"'{folder.relpath}' overlaps a folder the running "
                    f"user-managed Syncthing already syncs ({user_path}); "
                    "remove it there first."
                )


def _peer_devices(eligibility: EligibilityResult) -> list[PeerDevice]:
    return [
        PeerDevice(
            device_id=peer.syncthing_device_id,
            name=peer.name,
            address=peer_address(peer.tailscale_magic_dns),
        )
        for peer in syncable_peers(eligibility)
    ]


def render_configuration(
    eligibility: EligibilityResult | None = None,
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Render Syncthing config.xml + per-folder ignores from current state."""
    from openbase_coder_cli.sync_config import sync_folders

    device_id = ensure_identity()
    eligibility = eligibility or current_eligibility()
    peers = _peer_devices(eligibility)
    write_config(
        self_device_id=device_id,
        self_name=socket.gethostname(),
        peers=peers,
        folders=list(sync_folders(config_path)),
    )
    ignore_paths = update_all_ignores(config_path)
    return {
        "device_id": device_id,
        "peer_count": len(peers),
        "ignore_files": [str(path) for path in ignore_paths],
    }


def _service_definition():
    from openbase_coder_cli.services.registry import find_service

    return find_service(CODE_SYNC_SERVICE_NAME)


# Cross-device thread sync rides the code-sync transport (the thread-sync
# exchange folder is a product-state sync folder). Its workers live in the
# always-installed sync-workers service and gate on code_sync_enabled() at
# runtime, so enabling code sync only needs the Syncthing engine service.


def install_and_start_service() -> None:
    from openbase_coder_cli.services.launchd import install_service
    from openbase_coder_cli.services.registry import require_installation

    try:
        installation = require_installation()
        install_service(installation, _service_definition())
    except click.ClickException as exc:
        raise CodeSyncError(str(exc)) from exc


def restart_service_if_installed() -> None:
    from openbase_coder_cli.services.launchd import (
        launchctl_kickstart,
        launchctl_status,
    )

    service = _service_definition()
    if launchctl_status(service).get("installed"):
        launchctl_kickstart(service)


def stop_and_remove_service() -> bool:
    from openbase_coder_cli.services.launchd import remove_service

    return remove_service(_service_definition())


def enable_code_sync(
    *, force: bool = False, config_path: Path | None = None
) -> dict[str, Any]:
    """Turn code sync on: identity, config, ignores, service, registration."""
    eligibility = current_eligibility()
    if not eligibility.eligible and not force:
        raise CodeSyncError(eligibility.reason or "Code sync is not eligible.")

    from openbase_coder_cli.sync_config import sync_folders

    ensure_port_available()
    ensure_no_user_managed_overlap(sync_folders(config_path))

    from openbase_coder_cli.code_sync.install import ensure_syncthing_installed

    ensure_syncthing_installed()
    ensure_product_state_folders(config_path)
    rendered = render_configuration(eligibility, config_path=config_path)
    set_code_sync_enabled(True, config_path)
    SYNC_VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    install_and_start_service()

    # Advertise the new capabilities so peers can add this device.
    from openbase_coder_cli.services.cloud_registration import register_and_report

    report = register_and_report()
    return {
        "enabled": True,
        "eligible": eligibility.eligible,
        "eligible_reason": eligibility.reason,
        "registered": report.ok,
        **rendered,
    }


def disable_code_sync(config_path: Path | None = None) -> dict[str, Any]:
    """Turn code sync off; local data (versions, identity) is kept."""
    removed = stop_and_remove_service()
    set_code_sync_enabled(False, config_path)
    from openbase_coder_cli.services.cloud_registration import register_and_report

    register_and_report()
    return {"enabled": False, "service_removed": removed}


def apply_settings_change(config_path: Path | None = None) -> dict[str, Any]:
    """Re-render config/ignores after a settings mutation and reload sync."""
    if not code_sync_enabled(config_path):
        return {"applied": False, "reason": "code sync is disabled"}
    from openbase_coder_cli.sync_config import sync_folders

    ensure_no_user_managed_overlap(sync_folders(config_path))
    rendered = render_configuration(config_path=config_path)
    # syncthing runs with --no-restart; kick the service so the rendered
    # config.xml is reloaded, then request a rescan once it is back.
    restart_service_if_installed()
    try:
        SyncthingClient().rescan()
    except CodeSyncError:
        pass  # The restart itself rescans; REST may not be back up yet.
    return {"applied": True, **rendered}


def ensure_product_state_folders(config_path: Path | None = None) -> list[str]:
    """Add the first-class product-state folders when missing.

    Thread sync between devices and the Openbase-managed agent homes'
    skills (codex AND claude) ride code sync automatically — they are not
    something the user discovers and adds by hand.
    """
    from openbase_coder_cli.sync_config import (
        PRODUCT_STATE_RELPATHS,
        add_sync_folder,
        sync_folders,
    )

    existing = {folder.relpath for folder in sync_folders(config_path)}
    added: list[str] = []
    for relpath in PRODUCT_STATE_RELPATHS:
        if relpath in existing:
            continue
        (Path.home() / relpath).mkdir(parents=True, exist_ok=True)
        add_sync_folder(relpath, config_path)
        added.append(relpath)
    return added


def accept_pending_folders(config_path: Path | None = None) -> list[str]:
    """Adopt folders a paired peer offered, making console adds bidirectional.

    Folder configs are per-device, so a folder added through one machine's
    console would otherwise sit unshared on the peer. Syncthing records the
    peer's offer as a pending folder; adopt it when it is trustworthy:
    the offer's label must validate as a home-relative path AND the offered
    folder ID must equal the deterministic ID derived from that label —
    which proves the offer came from a code-sync peer using the same
    derivation, not an arbitrary share.
    """
    from openbase_coder_cli.sync_config import (
        add_sync_folder,
        folder_id_for_relpath,
        sync_folders,
        validate_relpath,
    )

    try:
        pending = SyncthingClient().pending_folders()
    except CodeSyncError:
        return []
    if not pending:
        return []

    known = {folder.folder_id for folder in sync_folders(config_path)}
    accepted: list[str] = []
    for folder_id, offer in pending.items():
        if folder_id in known:
            continue  # Configured already; share completes on next render.
        offered_by = offer.get("offeredBy") if isinstance(offer, dict) else None
        if not isinstance(offered_by, dict):
            continue
        for meta in offered_by.values():
            label = meta.get("label") if isinstance(meta, dict) else None
            if not isinstance(label, str) or not label:
                continue
            try:
                relpath = validate_relpath(label)
            except ValueError:
                continue
            if folder_id_for_relpath(relpath) != folder_id:
                continue
            add_sync_folder(relpath, config_path)
            accepted.append(relpath)
            break
    if accepted:
        apply_settings_change(config_path)
    return accepted


def versions_usage_bytes(versions_dir: Path | None = None) -> int:
    root = versions_dir or SYNC_VERSIONS_DIR
    total = 0
    if not root.is_dir():
        return total
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def purge_versions(versions_dir: Path | None = None) -> int:
    """Delete all staggered version copies; returns freed bytes."""
    import shutil

    root = versions_dir or SYNC_VERSIONS_DIR
    freed = versions_usage_bytes(root)
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    return freed
