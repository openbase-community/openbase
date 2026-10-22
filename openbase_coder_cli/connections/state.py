"""Local, non-secret state for managed connections: which proxies this
machine is running and a device-local audit log of what the CLI did.

Credentials are never written here; Cloud keeps the authoritative audit
trail, this log is for the person reading the machine."""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from openbase_coder_cli.paths import OPENBASE_BASE_DIR

CONNECTIONS_DIR = OPENBASE_BASE_DIR / "connections"
PROXIES_JSON_PATH = CONNECTIONS_DIR / "proxies.json"
AUDIT_LOG_PATH = CONNECTIONS_DIR / "audit.log"


@dataclass(frozen=True)
class RunningProxy:
    kind: str
    name: str
    port: int
    pid: int
    role: str
    started_at: str
    url: str


def _ensure_dir() -> None:
    CONNECTIONS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONNECTIONS_DIR, 0o700)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_proxies() -> list[RunningProxy]:
    """Running proxies, dropping entries whose process is gone."""
    try:
        raw = json.loads(PROXIES_JSON_PATH.read_text())
    except (OSError, ValueError):
        return []
    proxies = []
    for item in raw if isinstance(raw, list) else []:
        try:
            proxy = RunningProxy(**item)
        except TypeError:
            continue
        if _pid_alive(proxy.pid):
            proxies.append(proxy)
    return proxies


def _save_proxies(proxies: list[RunningProxy]) -> None:
    _ensure_dir()
    tmp = PROXIES_JSON_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps([asdict(proxy) for proxy in proxies], indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(PROXIES_JSON_PATH)


def register_proxy(
    kind: str, name: str, port: int, role: str, url: str
) -> RunningProxy:
    proxy = RunningProxy(
        kind=kind,
        name=name,
        port=port,
        pid=os.getpid(),
        role=role,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        url=url,
    )
    proxies = [
        item for item in load_proxies() if not (item.kind == kind and item.name == name)
    ]
    _save_proxies([*proxies, proxy])
    return proxy


def unregister_proxy(kind: str, name: str, pid: int | None = None) -> None:
    proxies = [
        item
        for item in load_proxies()
        if not (
            item.kind == kind and item.name == name and (pid is None or item.pid == pid)
        )
    ]
    _save_proxies(proxies)


def find_proxy(kind: str, name: str) -> RunningProxy | None:
    return next(
        (item for item in load_proxies() if item.kind == kind and item.name == name),
        None,
    )


def stop_proxy(kind: str, name: str, *, timeout: float = 5.0) -> bool:
    proxy = find_proxy(kind, name)
    if proxy is None:
        return False
    os.kill(proxy.pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_alive(proxy.pid):
        time.sleep(0.1)
    unregister_proxy(kind, name, proxy.pid)
    return True


def audit(kind: str, name: str, action: str, detail: str = "", **extra: Any) -> None:
    """Append one line to the device-local audit log (no secrets)."""
    _ensure_dir()
    record = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": kind,
        "name": name,
        "action": action,
        "detail": detail,
        "pid": os.getpid(),
        **extra,
    }
    with AUDIT_LOG_PATH.open("a") as handle:
        handle.write(json.dumps(record) + "\n")
    os.chmod(AUDIT_LOG_PATH, 0o600)
