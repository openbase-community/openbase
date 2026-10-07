"""Openbase Sync daemon integration (``openbase-syncd``).

The daemon is the hub/edge mirror that keeps a user's computers in sync. It
owns its state under ``~/.openbase/sync`` and exposes a JSON-lines control API
on a unix socket. This module is the thin client the CLI, the API views and
health checks use, plus the helpers that edit the ``[[roots]]`` of its config
file; it never implements sync logic.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

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

# Product folders: state other Openbase features exchange between computers
# by having it mirrored. Thread device sync writes snapshots into the thread
# exchange; skills sync shares the personal skills directory (plus the
# folders its skills link to, see ``skills_sync``). Written home-relative so
# the same config works on computers whose home directories differ.
PERSONAL_SKILLS_ROOT = "~/.agents/skills"


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
        lines += render_roots_toml(self.roots)
        return "\n".join(lines) + "\n"


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_roots_toml(roots: Iterable[dict[str, Any]]) -> list[str]:
    """``[[roots]]`` blocks (id, path, optional pins) as TOML lines."""
    lines: list[str] = []
    for root in roots:
        lines += [
            "",
            "[[roots]]",
            f"id = {_toml_string(str(root['id']))}",
            f"path = {_toml_string(str(root['path']))}",
        ]
        pins = [str(pin) for pin in root.get("pins") or [] if str(pin).strip()]
        if pins:
            lines.append(
                "pins = [" + ", ".join(_toml_string(pin) for pin in pins) + "]"
            )
    return lines


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
    if sys.platform == "darwin" and _is_macho(tmp):
        subprocess.run(
            ["codesign", "-s", "-", "-f", str(tmp)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    os.replace(tmp, dest)
    return dest


def _is_macho(path: Path) -> bool:
    return path.read_bytes()[:4] in {
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
    }


def _config_path(config_path: Path | None) -> Path:
    return Path(config_path) if config_path is not None else SYNC_DAEMON_CONFIG_PATH


def is_configured(config_path: Path | None = None) -> bool:
    return _config_path(config_path).is_file()


_SUMMARY_KEYS = (
    "device_id",
    "sync_group",
    "role",
    "listen_hot",
    "peer_hot",
    "log_level",
)


def _load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def read_config_summary(config_path: Path | None = None) -> dict:
    """The fields the UI shows; never raises for a missing or broken file."""
    config_path = _config_path(config_path)
    summary: dict = {"configured": False, "roots": []}
    if not config_path.is_file():
        return summary
    summary["configured"] = True
    try:
        data = _load_config(config_path)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        summary["error"] = f"unreadable config: {exc}"
        return summary
    for key in _SUMMARY_KEYS:
        if isinstance(data.get(key), str):
            summary[key] = data[key]
    summary["roots"] = _roots_from_config(data)
    return summary


def _roots_from_config(data: dict[str, Any]) -> list[dict[str, Any]]:
    roots: list[dict[str, Any]] = []
    for raw in data.get("roots") or []:
        if not isinstance(raw, dict):
            continue
        root: dict[str, Any] = {
            "id": str(raw.get("id") or ""),
            "path": str(raw.get("path") or ""),
        }
        pins = raw.get("pins")
        if isinstance(pins, list) and pins:
            root["pins"] = [str(pin) for pin in pins]
        roots.append(root)
    return roots


def configured_roots(config_path: Path | None = None) -> list[dict[str, Any]]:
    """The ``[[roots]]`` of the config (empty when unconfigured or unreadable)."""
    config_path = _config_path(config_path)
    if not config_path.is_file():
        return []
    try:
        return _roots_from_config(_load_config(config_path))
    except (OSError, tomllib.TOMLDecodeError):
        return []


def write_config(config: SyncDaemonConfig, config_path: Path | None = None) -> Path:
    config_path = _config_path(config_path)
    _write_config_text(config.to_toml(), config_path)
    return config_path


def _write_config_text(text: str, config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(config_path)


# --- roots -----------------------------------------------------------------


def expand_root_path(path: str | Path) -> Path:
    """The absolute directory a root path names (``~/`` is the home dir)."""
    return Path(path).expanduser().resolve()


def home_relative_root_path(path: str | Path) -> str:
    """``~/...`` for a directory under the home dir, else its absolute path.

    The daemon matches roots by home-relative path, so writing them this way
    keeps one config valid on computers whose home directories differ.
    """
    resolved = expand_root_path(path)
    home = Path.home().resolve()
    try:
        rel = resolved.relative_to(home)
    except ValueError:
        return str(resolved)
    return "~" if not rel.parts else "~/" + rel.as_posix()


def root_entry(path: str | Path, pins: Iterable[str] = ()) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": root_id_for_path(path),
        "path": home_relative_root_path(path),
    }
    pins = [pin for pin in pins if pin]
    if pins:
        entry["pins"] = pins
    return entry


def roots_overlap(a: str | Path, b: str | Path) -> bool:
    """True when one root contains the other (or they are the same folder)."""
    path_a, path_b = expand_root_path(a), expand_root_path(b)
    return path_a == path_b or path_a in path_b.parents or path_b in path_a.parents


def path_is_synced(path: str | Path, config_path: Path | None = None) -> bool:
    """Whether ``path`` lies inside a configured root (unconfigured: False)."""
    target = expand_root_path(path)
    for root in configured_roots(config_path):
        if not root.get("path"):
            continue
        base = expand_root_path(root["path"])
        if target == base or base in target.parents:
            return True
    return False


def set_roots(roots: Iterable[dict[str, Any]], config_path: Path | None = None) -> Path:
    """Replace every ``[[roots]]`` block, keeping the rest of the file as is.

    The config may carry keys this CLI does not manage (placement tuning,
    command relay allowlists), so only the root blocks are rewritten.
    """
    config_path = _config_path(config_path)
    if not config_path.is_file():
        raise SyncDaemonError(
            f"{config_path} does not exist; run `openbase-coder sync-daemon "
            "configure` first"
        )
    kept: list[str] = []
    in_root = False
    for line in config_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "[[roots]]":
            in_root = True
            continue
        if in_root and stripped.startswith("["):
            in_root = False
        if not in_root:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    text = "\n".join(kept + render_roots_toml(roots)) + "\n"
    tomllib.loads(text)  # never write a file the daemon cannot parse
    _write_config_text(text, config_path)
    return config_path


@dataclass
class RootChange:
    added: list[dict[str, Any]] = field(default_factory=list)
    replaced: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.replaced)


def plan_root_additions(
    existing: list[dict[str, Any]],
    paths: Iterable[str | Path],
    *,
    replace_nested: bool = False,
) -> tuple[list[dict[str, Any]], RootChange]:
    """The root list after adding ``paths``, and what changed.

    Roots must not nest: the daemon watches each root on its own, so a root
    inside another would mirror the same files twice. A path already covered
    by an existing root is skipped. A path that would contain existing roots
    is skipped too, unless ``replace_nested`` is set, in which case the inner
    roots are dropped in favour of the outer one.
    """
    roots = [dict(root) for root in existing]
    change = RootChange()
    for raw in paths:
        entry = root_entry(raw)
        target = expand_root_path(entry["path"])
        covering = [
            root
            for root in roots
            if root.get("path")
            and (
                expand_root_path(root["path"]) == target
                or expand_root_path(root["path"]) in target.parents
            )
        ]
        if covering:
            change.skipped.append(
                (entry["path"], f"already inside root {covering[0]['path']}")
            )
            continue
        nested = [
            root
            for root in roots
            if root.get("path") and target in expand_root_path(root["path"]).parents
        ]
        if nested and not replace_nested:
            inner = ", ".join(root["path"] for root in nested)
            change.skipped.append(
                (
                    entry["path"],
                    f"would contain existing root(s) {inner}; pass "
                    "--replace-nested to replace them",
                )
            )
            continue
        for root in nested:
            roots.remove(root)
            change.replaced.append(root)
        roots.append(entry)
        change.added.append(entry)
    return roots, change


def add_roots(
    paths: Iterable[str | Path],
    *,
    replace_nested: bool = False,
    config_path: Path | None = None,
) -> RootChange:
    """Add roots to the config file (see ``plan_root_additions``)."""
    roots, change = plan_root_additions(
        configured_roots(config_path), paths, replace_nested=replace_nested
    )
    if change.changed:
        set_roots(roots, config_path)
    return change


def remove_roots(
    paths: Iterable[str | Path], config_path: Path | None = None
) -> list[dict[str, Any]]:
    """Remove the roots naming exactly ``paths``; returns the removed roots."""
    targets = {expand_root_path(path) for path in paths}
    roots = configured_roots(config_path)
    removed = [
        root
        for root in roots
        if root.get("path") and expand_root_path(root["path"]) in targets
    ]
    if removed:
        set_roots([root for root in roots if root not in removed], config_path)
    return removed


def thread_sync_root() -> str:
    """The thread exchange folder as a root path (``~/.openbase/thread-sync``)."""
    return home_relative_root_path(OPENBASE_BASE_DIR / "thread-sync")


def product_folder_roots() -> list[str]:
    """Thread exchange, personal skills and the folders skills link to."""
    from openbase_coder_cli import skills_sync

    roots = [thread_sync_root(), PERSONAL_SKILLS_ROOT]
    for source in skills_sync.linked_source_paths():
        candidate = home_relative_root_path(source)
        if candidate not in roots:
            roots.append(candidate)
    return roots


def service_installed() -> bool:
    from openbase_coder_cli.services.launchd import launchctl_status
    from openbase_coder_cli.services.registry import find_service

    try:
        return bool(
            launchctl_status(find_service(SYNC_DAEMON_SERVICE_NAME)).get("installed")
        )
    except Exception:  # noqa: BLE001 - a status probe must never break callers
        return False


def restart_service_if_installed() -> bool:
    """Restart the daemon so it re-reads its roots; False when not installed."""
    if not service_installed():
        return False
    from openbase_coder_cli.services.installation import InstallationConfig
    from openbase_coder_cli.services.launchd import install_service
    from openbase_coder_cli.services.registry import find_service

    install_service(InstallationConfig.load(), find_service(SYNC_DAEMON_SERVICE_NAME))
    return True


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


USER_BIN_DIR = Path.home() / ".local" / "bin"
CLI_TOOLS = ("edge", "openbase-sync")


def link_cli_tools(
    user_bin: Path | None = None,
    package_bin: Path | None = None,
    manual_bin: Path | None = None,
) -> list[Path]:
    """Put `edge` and `openbase-sync` on PATH next to `openbase-coder`.

    Links point at the packaged binaries when present (so self-update moves
    them), else at a manual `install-binary` copy. Existing files that are not
    our symlinks are left alone.
    """
    from openbase_coder_cli.paths import STANDALONE_CURRENT_DIR

    user_bin = user_bin or USER_BIN_DIR
    package_bin = package_bin or (STANDALONE_CURRENT_DIR / "bin")
    manual_bin = manual_bin or OPENBASE_BIN_DIR
    linked: list[Path] = []
    for name in CLI_TOOLS:
        target = package_bin / name if (package_bin / name).exists() else manual_bin / name
        if not target.exists():
            continue
        link = user_bin / name
        if link.exists() or link.is_symlink():
            if not link.is_symlink():
                continue  # a file of the user's: never replace it
            if Path(os.readlink(link)) == target:
                continue
            link.unlink()
        user_bin.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        linked.append(link)
    return linked
