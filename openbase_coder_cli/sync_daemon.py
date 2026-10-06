"""Openbase Sync daemon integration (``openbase-syncd``).

The daemon is the hub/edge mirror that replaces the Syncthing-based code sync.
It owns its own config and state under ``~/.openbase/sync`` and exposes a
JSON-lines control API on a unix socket. This module is the thin client the
CLI, the API views and health checks use; it never implements sync logic.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from openbase_coder_cli.paths import OPENBASE_BASE_DIR, OPENBASE_BIN_DIR

SYNC_DAEMON_DIR = OPENBASE_BASE_DIR / "sync"
SYNC_DAEMON_CONFIG_PATH = SYNC_DAEMON_DIR / "config.toml"
SYNC_DAEMON_SOCKET_PATH = SYNC_DAEMON_DIR / "syncd.sock"
SYNC_DAEMON_SERVICE_NAME = "sync-daemon"
SYNC_DAEMON_BINARY_NAME = "openbase-syncd"
SYNC_CTL_BINARY_NAME = "openbase-sync"
SYNC_EDGE_BINARY_NAME = "edge"  # hub-side relay for display-bound commands
DEFAULT_HOT_PORT = 22100
DEFAULT_BULK_PORT = 22101


class SyncDaemonError(RuntimeError):
    """The daemon is not running, not configured, or answered with an error."""


@dataclass
class SyncDaemonConfig:
    device_id: str
    sync_group: str
    role: str  # hub | edge
    pair_secret: str
    roots: list[dict[str, str]] = field(default_factory=list)
    listen_hot: str = ""
    listen_bulk: str = ""
    peer_hot: str = ""
    peer_bulk: str = ""
    debounce_ms: int = 15
    log_level: str = "info"
    low_water_mb: int = 10240
    anchor: str = "hub"  # hub | edge: the side that holds every file in full

    def to_toml(self) -> str:
        def q(value: str) -> str:
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

        lines = [
            f"device_id = {q(self.device_id)}",
            f"sync_group = {q(self.sync_group)}",
            f"role = {q(self.role)}",
            f"pair_secret = {q(self.pair_secret)}",
            f"state_dir = {q(str(SYNC_DAEMON_CONFIG_PATH.parent))}",
            f"socket = {q(str(SYNC_DAEMON_SOCKET_PATH))}",
            f"debounce_ms = {int(self.debounce_ms)}",
            f"log_level = {q(self.log_level)}",
        ]
        if self.role == "hub":
            lines.append(f"listen_hot = {q(self.listen_hot)}")
            lines.append(f"listen_bulk = {q(self.listen_bulk)}")
        else:
            lines.append(f"peer_hot = {q(self.peer_hot)}")
            lines.append(f"peer_bulk = {q(self.peer_bulk)}")
        lines += [
            "",
            "[placement]",
            f"low_water_mb = {int(self.low_water_mb)}",
            f"anchor = {q(self.anchor)}",
        ]
        for root in self.roots:
            lines += [
                "",
                "[[roots]]",
                f"id = {q(root['id'])}",
                f"path = {q(root['path'])}",
            ]
        return "\n".join(lines) + "\n"


def root_id_for_path(path: str | Path) -> str:
    """A stable root id derived from the path relative to the home directory.

    ``~/Projects/friendforce/data`` becomes ``projects-friendforce-data``, so
    two roots whose last component is ``data`` do not collide. Paths outside
    the home directory use their full path. The id must match on both sides,
    which it does when the layout is identical (the daemon requires that).
    """
    resolved = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    try:
        rel = resolved.relative_to(home)
        parts = rel.parts
    except ValueError:
        parts = resolved.parts[1:]
    if not parts:
        parts = ("home",)
    raw = "-".join(parts)
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in raw).lower()
    return cleaned.strip("-") or "root"


def install_executable(src: Path, dest: Path) -> Path:
    """Install an executable atomically: write beside the destination, then rename.

    Overwriting a Mach-O binary in place while a process runs from it makes
    the next launch die with SIGKILL on macOS (the kernel's signature cache
    sees a modified file). A new inode plus an ad-hoc signature avoids that.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".new")
    shutil.copy2(src, tmp)
    tmp.chmod(0o755)
    if sys.platform == "darwin":
        subprocess.run(
            ["codesign", "-s", "-", "-f", str(tmp)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    os.replace(tmp, dest)
    return dest


def _config_path(config_path: Path | None) -> Path:
    return Path(config_path) if config_path is not None else SYNC_DAEMON_CONFIG_PATH


def is_configured(config_path: Path | None = None) -> bool:
    return _config_path(config_path).is_file()


def read_config_summary(config_path: Path | None = None) -> dict:
    """A small, dependency-free TOML reader for the fields the UI shows."""
    config_path = _config_path(config_path)
    summary: dict = {"configured": False, "roots": []}
    if not config_path.is_file():
        return summary
    summary["configured"] = True
    current_root: dict | None = None
    for raw in config_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[[roots]]":
            current_root = {}
            summary["roots"].append(current_root)
            continue
        if line.startswith("["):
            current_root = None
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"')
        if current_root is not None:
            current_root[key] = value
        elif key in {
            "device_id",
            "sync_group",
            "role",
            "listen_hot",
            "peer_hot",
            "log_level",
        }:
            summary[key] = value
    return summary


def write_config(config: SyncDaemonConfig, config_path: Path | None = None) -> Path:
    config_path = _config_path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".toml.tmp")
    tmp.write_text(config.to_toml(), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(config_path)
    return config_path


def new_pair_secret() -> str:
    return secrets.token_hex(16)


def default_device_id() -> str:
    """Reuse the cloud device id when known so the registry and the daemon agree."""
    try:
        from openbase_coder_cli.services.cloud_registration import local_device_id

        device_id = local_device_id()
        if device_id:
            return str(device_id)
    except Exception:  # noqa: BLE001 - registration is optional here
        pass
    return "desktop-" + secrets.token_hex(6)


def daemon_binary_candidates() -> list[Path]:
    return [OPENBASE_BIN_DIR / SYNC_DAEMON_BINARY_NAME]


def ctl_binary_candidates() -> list[Path]:
    return [OPENBASE_BIN_DIR / SYNC_CTL_BINARY_NAME]


def resolve_socket_path(configured: Path) -> Path:
    """The socket to dial for a configured path.

    macOS limits AF_UNIX paths to 104 bytes, so when the configured path is too
    long the daemon binds a short ``/tmp`` socket and writes its location into
    ``<configured>.path``; clients follow that pointer when present.
    """
    pointer = configured.with_name(configured.name + ".path")
    try:
        actual = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return configured
    return Path(actual) if actual else configured


class SyncDaemonClient:
    """JSON-lines client for the daemon's unix control socket."""

    def __init__(self, socket_path: Path | None = None, timeout: float = 2.0):
        self.socket_path = (
            Path(socket_path) if socket_path is not None else SYNC_DAEMON_SOCKET_PATH
        )
        self.timeout = timeout

    def call(self, op: str, **fields) -> dict:
        request = {"op": op, **{k: v for k, v in fields.items() if v is not None}}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self.timeout)
                conn.connect(str(resolve_socket_path(self.socket_path)))
                conn.sendall((json.dumps(request) + "\n").encode("utf-8"))
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except (OSError, socket.timeout) as exc:
            raise SyncDaemonError(
                f"sync daemon unreachable at {self.socket_path}: {exc}"
            ) from exc
        if not buf:
            raise SyncDaemonError("sync daemon closed the connection without answering")
        try:
            response = json.loads(buf.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SyncDaemonError(f"sync daemon returned invalid JSON: {exc}") from exc
        if not response.get("ok"):
            raise SyncDaemonError(response.get("error") or "sync daemon error")
        return response

    def status(self) -> dict:
        return self.call("status").get("data") or {}

    def metrics(self) -> dict:
        return self.call("metrics").get("data") or {}

    def conflicts(self, root: str | None = None) -> list[dict]:
        return self.call("conflicts", root=root).get("data") or []

    def resolve(self, conflict_id: int, choice: str) -> None:
        if choice not in {"a", "b"}:
            raise SyncDaemonError("choice must be 'a' (keep mine) or 'b' (take theirs)")
        self.call("resolve", id=int(conflict_id), choice=choice)

    def barrier(
        self,
        kind: str,
        path: str | None = None,
        root: str | None = None,
        timeout_ms: int = 300,
    ) -> dict:
        response = self.call(
            "barrier", kind=kind, path=path, root=root, timeout_ms=timeout_ms
        )
        return {"result": response.get("result"), "lag": response.get("lag", 0)}

    def stubs(self, root: str | None = None) -> list[dict]:
        return self.call("stubs", root=root).get("data") or []

    def hydrate(self, path: str) -> dict:
        return self.call("hydrate", path=path).get("data") or {}

    def held_deletes(self, root: str) -> list[str]:
        return self.call("held-deletes", root=root).get("data") or []

    def release_deletes(self, root: str) -> int:
        return int(self.call("release-deletes", root=root).get("data") or 0)

    def discard_deletes(self, root: str) -> int:
        return int(self.call("discard-deletes", root=root).get("data") or 0)


def reachable(socket_path: Path | None = None) -> bool:
    try:
        SyncDaemonClient(socket_path, timeout=0.5).call("status")
        return True
    except SyncDaemonError:
        return False
