"""Manage the openbase-owned Syncthing instance for code-sync.

The Syncthing config dir is ``~/.openbase/code-sync/``: openbase-coder owns
``config.xml`` end to end (devices come from the cloud device registry,
transport is pinned to Tailscale addresses, discovery/relays/NAT traversal
are disabled). The GUI/REST API binds to 127.0.0.1:8385 so a user-managed
Syncthing on the default 8384 is never disturbed.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import httpx

from openbase_coder_cli.code_sync import CodeSyncError
from openbase_coder_cli.paths import CODE_SYNC_DIR, SYNC_VERSIONS_DIR
from openbase_coder_cli.sync_config import SyncFolder

GUI_ADDRESS = "127.0.0.1:8385"
REST_BASE_URL = f"http://{GUI_ADDRESS}"
SYNC_LISTEN_PORT = 22000
DEVICE_ID_FILENAME = "device-id"
CONFIG_XML_FILENAME = "config.xml"
CERT_FILENAME = "cert.pem"
# Staggered versioning: history thins automatically; bound is time (30 days).
VERSIONS_MAX_AGE_SECONDS = 30 * 24 * 3600
# Absolute pause floor for folders and the config/index home. Overrides
# Syncthing's percentage default, which scales with disk size.
MIN_DISK_FREE_MB = 2048
REST_TIMEOUT_SECONDS = 10
# v1 prints "Device ID: <id>"; v2 prints "... (device=<id> log.pkg=...)".
_DEVICE_ID_RE = re.compile(r"(?:Device ID:\s*|device=)([A-Z2-7]{7}(?:-[A-Z2-7]{7}){7})")


@dataclass(frozen=True)
class PeerDevice:
    """A remote sync peer from the cloud device registry."""

    device_id: str
    name: str
    address: str


def resolve_syncthing_binary() -> str:
    """Locate syncthing: managed install, PATH, then Homebrew dirs."""
    from openbase_coder_cli.code_sync.install import managed_syncthing_path

    managed = managed_syncthing_path()
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed)
    path = shutil.which("syncthing")
    if path:
        return path
    for fallback in (
        Path("/opt/homebrew/bin/syncthing"),
        Path("/usr/local/bin/syncthing"),
    ):
        if fallback.is_file():
            return str(fallback)
    raise click.ClickException(
        "Could not find 'syncthing'. Run 'openbase-coder sync enable' to "
        "download it, or install it manually (e.g. 'brew install syncthing')."
    )


def stored_device_id(config_dir: Path = CODE_SYNC_DIR) -> str | None:
    """This machine's Syncthing device ID, without spawning syncthing."""
    try:
        value = (config_dir / DEVICE_ID_FILENAME).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    return value or None


def ensure_identity(config_dir: Path = CODE_SYNC_DIR, binary: str | None = None) -> str:
    """Create the Syncthing key/cert if needed and return the device ID.

    ``syncthing generate`` is idempotent: it refuses to overwrite existing
    keys and prints the device ID either way. Nothing secret leaves
    ``config_dir``; only the (public) device ID is persisted alongside it.
    """
    existing = stored_device_id(config_dir)
    if existing and (config_dir / CERT_FILENAME).is_file():
        return existing

    syncthing = binary or resolve_syncthing_binary()
    config_dir.mkdir(parents=True, exist_ok=True)
    # --home sets config+data together (Syncthing v2 requires both, and v2
    # dropped v1's --no-default-folder). A default folder in the generated
    # config.xml is irrelevant anyway: write_config replaces it wholesale —
    # generate only exists to mint the key/cert identity.
    result = subprocess.run(
        [syncthing, "generate", f"--home={config_dir}"],
        capture_output=True,
        text=True,
        check=False,
    )
    device_id = _device_id_from_output(result.stdout + "\n" + result.stderr)
    if result.returncode != 0 and device_id is None:
        raise CodeSyncError(
            f"syncthing generate failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    if device_id is None:
        raise CodeSyncError(
            "syncthing generate did not report a device ID; "
            f"output: {result.stdout.strip()!r}"
        )
    (config_dir / DEVICE_ID_FILENAME).write_text(device_id + "\n", encoding="utf-8")
    return device_id


def _device_id_from_output(text: str) -> str | None:
    match = _DEVICE_ID_RE.search(text)
    return match.group(1) if match else None


def existing_api_key(config_dir: Path = CODE_SYNC_DIR) -> str | None:
    """The REST API key from the rendered config.xml, if present."""
    config_path = config_dir / CONFIG_XML_FILENAME
    try:
        root = ET.parse(config_path).getroot()
    except (FileNotFoundError, OSError, ET.ParseError):
        return None
    element = root.find("./gui/apikey")
    if element is None or not (element.text or "").strip():
        return None
    return element.text.strip()


def peer_address(tailscale_magic_dns: str) -> str:
    host = tailscale_magic_dns.rstrip(".")
    return f"tcp://{host}:{SYNC_LISTEN_PORT}"


def existing_folder_types(config_dir: Path = CODE_SYNC_DIR) -> dict[str, str]:
    """Folder types from the current config.xml, so re-renders preserve the
    write lease instead of resetting every folder to send-receive."""
    config_path = config_dir / CONFIG_XML_FILENAME
    try:
        root = ET.parse(config_path).getroot()
    except (FileNotFoundError, OSError, ET.ParseError):
        return {}
    return {
        element.get("id", ""): element.get("type", "")
        for element in root.findall("./folder")
        if element.get("id")
    }


def render_config_xml(
    *,
    self_device_id: str,
    self_name: str,
    api_key: str,
    peers: list[PeerDevice],
    folders: list[SyncFolder],
    home: Path | None = None,
    versions_dir: Path | None = None,
    folder_types: dict[str, str] | None = None,
) -> str:
    """Render the full Syncthing config.xml owned by openbase-coder."""
    home = home or Path.home()
    versions_dir = versions_dir or SYNC_VERSIONS_DIR
    folder_types = folder_types or {}
    root = ET.Element("configuration", version="37")

    for folder in folders:
        current_type = folder_types.get(folder.folder_id)
        folder_element = ET.SubElement(
            root,
            "folder",
            id=folder.folder_id,
            label=folder.relpath,
            path=str(folder.absolute_path(home)),
            # Preserve the lease's receive-only flips across re-renders.
            type=(
                current_type
                if current_type in ("sendreceive", "receiveonly")
                else "sendreceive"
            ),
            rescanIntervalS="3600",
            fsWatcherEnabled="true",
            fsWatcherDelayS="10",
            autoNormalize="true",
            # Keep case-conflict detection on (the default). A macOS
            # (case-insensitive) peer syncing with a Linux DevSpace
            # (case-sensitive) can otherwise silently collide files that
            # differ only in case; false = Syncthing detects and flags them.
            caseSensitiveFS="false",
        )
        for device_id in [self_device_id, *(peer.device_id for peer in peers)]:
            ET.SubElement(folder_element, "device", id=device_id)
        ET.SubElement(folder_element, "minDiskFree", unit="MB").text = str(
            MIN_DISK_FREE_MB
        )
        versioning = ET.SubElement(folder_element, "versioning", type="staggered")
        ET.SubElement(
            versioning, "param", key="maxAge", val=str(VERSIONS_MAX_AGE_SECONDS)
        )
        ET.SubElement(
            versioning,
            "param",
            key="versionsPath",
            val=str(versions_dir / folder.folder_id),
        )

    self_element = ET.SubElement(
        root,
        "device",
        id=self_device_id,
        name=self_name,
        compression="metadata",
    )
    ET.SubElement(self_element, "address").text = "dynamic"
    for peer in peers:
        peer_element = ET.SubElement(
            root, "device", id=peer.device_id, name=peer.name, compression="metadata"
        )
        ET.SubElement(peer_element, "address").text = peer.address

    gui = ET.SubElement(root, "gui", enabled="true", tls="false")
    ET.SubElement(gui, "address").text = GUI_ADDRESS
    ET.SubElement(gui, "apikey").text = api_key

    options = ET.SubElement(root, "options")
    ET.SubElement(options, "listenAddress").text = f"tcp://0.0.0.0:{SYNC_LISTEN_PORT}"
    for key, value in (
        ("globalAnnounceEnabled", "false"),
        ("localAnnounceEnabled", "false"),
        ("relaysEnabled", "false"),
        ("natEnabled", "false"),
        ("crashReportingEnabled", "false"),
        ("urAccepted", "-1"),
        ("startBrowser", "false"),
    ):
        ET.SubElement(options, key).text = value
    # Syncthing's default floor is 1% of the disk, which pauses sync with
    # ~18 GiB still free on a 2 TB drive; the home floor guards the index
    # database and is the one that stalled folders in practice.
    ET.SubElement(options, "minHomeDiskFree", unit="MB").text = str(MIN_DISK_FREE_MB)

    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def write_config(
    *,
    self_device_id: str,
    self_name: str,
    peers: list[PeerDevice],
    folders: list[SyncFolder],
    config_dir: Path = CODE_SYNC_DIR,
    home: Path | None = None,
    versions_dir: Path | None = None,
) -> Path:
    """Write config.xml, keeping the persisted REST API key stable."""
    api_key = existing_api_key(config_dir) or secrets.token_hex(16)
    content = render_config_xml(
        self_device_id=self_device_id,
        self_name=self_name,
        api_key=api_key,
        peers=peers,
        folders=folders,
        home=home,
        versions_dir=versions_dir,
        folder_types=existing_folder_types(config_dir),
    )
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / CONFIG_XML_FILENAME
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=config_dir, delete=False
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, config_path)
    return config_path


class SyncthingClient:
    """Minimal REST client for the managed Syncthing instance."""

    def __init__(
        self,
        base_url: str = REST_BASE_URL,
        api_key: str | None = None,
        config_dir: Path = CODE_SYNC_DIR,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        key = api_key or existing_api_key(config_dir)
        if not key:
            raise CodeSyncError("No Syncthing API key found; enable code sync first.")
        self._headers = {"X-API-Key": key}

    def _request(self, method: str, path: str, *, json_body: Any | None = None) -> Any:
        try:
            response = httpx.request(
                method,
                f"{self._base_url}{path}",
                headers=self._headers,
                json=json_body,
                timeout=REST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise CodeSyncError(f"Syncthing REST call failed: {exc}") from exc
        if response.status_code >= 400:
            raise CodeSyncError(
                f"Syncthing REST {method} {path} returned "
                f"{response.status_code}: {response.text[:200]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def system_status(self) -> dict[str, Any]:
        return self._request("GET", "/rest/system/status") or {}

    def folder_status(self, folder_id: str) -> dict[str, Any]:
        return self._request("GET", f"/rest/db/status?folder={folder_id}") or {}

    def folder_completion(
        self, folder_id: str, device_id: str | None = None
    ) -> dict[str, Any]:
        query = f"folder={folder_id}"
        if device_id:
            query += f"&device={device_id}"
        return self._request("GET", f"/rest/db/completion?{query}") or {}

    def folder_config(self, folder_id: str) -> dict[str, Any]:
        return self._request("GET", f"/rest/config/folders/{folder_id}") or {}

    def set_folder_type(self, folder_id: str, folder_type: str) -> None:
        if folder_type not in ("sendreceive", "receiveonly"):
            raise CodeSyncError(f"Unsupported folder type: {folder_type}")
        self._request(
            "PATCH",
            f"/rest/config/folders/{folder_id}",
            json_body={"type": folder_type},
        )

    def rescan(self, folder_id: str | None = None) -> None:
        query = f"?folder={folder_id}" if folder_id else ""
        self._request("POST", f"/rest/db/scan{query}")

    def connections(self) -> dict[str, Any]:
        """Per-device connection state (``connections`` key of the API)."""
        payload = self._request("GET", "/rest/system/connections") or {}
        value = payload.get("connections")
        return value if isinstance(value, dict) else {}

    def pending_folders(self) -> dict[str, Any]:
        """Folders peers offered that are not in our config yet."""
        payload = self._request("GET", "/rest/cluster/pending/folders")
        return payload if isinstance(payload, dict) else {}

    def system_errors(self) -> list[dict[str, Any]]:
        """Recent engine-level errors (e.g. disk-full folder stalls)."""
        payload = self._request("GET", "/rest/system/error") or {}
        errors = payload.get("errors") if isinstance(payload, dict) else None
        return [entry for entry in errors or [] if isinstance(entry, dict)]

    def latest_event_time(self, event_type: str) -> str | None:
        """RFC3339 time of the most recent buffered event of a type, if any."""
        events = (
            self._request(
                "GET",
                f"/rest/events?events={event_type}&since=0&limit=1&timeout=0",
            )
            or []
        )
        if not isinstance(events, list) or not events:
            return None
        value = events[-1].get("time")
        return value if isinstance(value, str) else None
