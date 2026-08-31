from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx

from openbase_coder_cli.file_lock import LOCK_EX, LOCK_UN, flock
from openbase_coder_cli.paths import (
    DEFAULT_LOG_DIR,
    LAUNCHD_WRAPPER_DIR,
    PLIST_DIR,
    PUBLISHED_SERVICES_PATH,
)
from openbase_coder_cli.runtime import stable_runtime_package

DYNAMIC_PORT_MIN = 49152
DYNAMIC_PORT_MAX = 65535
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
RESERVED_NAMES = {"api", "livekit", "openbase", "service", "services"}
LAUNCHD_LABEL_PREFIX = "com.openbase.coder.published-service"
HEALTH_PATH = "/.openbase-service-health"
MODE_DYNAMIC = "dynamic"
MODE_PORTLESS = "portless"
PORTLESS_TAILNET_PORT = 80
PORTLESS_PATH_PREFIX = "/services/"
PORTLESS_LAUNCHD_NAME = "portless-dispatcher"
_REGISTRY_LOCK_DEPTH: ContextVar[int] = ContextVar(
    "published_service_registry_lock_depth", default=0
)


@dataclass(frozen=True)
class PublishedService:
    name: str
    local_port: int
    tailnet_port: int
    proxy_port: int
    persistent: bool = False
    pid: int | None = None
    mode: str = MODE_DYNAMIC

    @property
    def target(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}"

    def serve_rule(self) -> dict[str, Any]:
        if self.mode == MODE_PORTLESS:
            return {"kind": "portless-dispatcher", "proxy_port": self.proxy_port}
        return {
            "kind": "published-dynamic",
            "tailnet_port": self.tailnet_port,
            "proxy_port": self.proxy_port,
        }

    @property
    def base_path(self) -> str:
        if self.mode == MODE_PORTLESS:
            return f"{PORTLESS_PATH_PREFIX}{self.name}/"
        return f"/{self.name}/"


@dataclass(frozen=True)
class ServiceRegistry:
    services: tuple[PublishedService, ...] = ()
    last_applied_serve_hash: str | None = None


def validate_name(value: str) -> str:
    name = value.strip().lower()
    if name.endswith(".local"):
        raise ValueError(
            ".local is reserved for multicast DNS; use a short name instead."
        )
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "Service names must start with a letter and contain only lowercase "
            "letters, numbers, and hyphens (40 characters maximum)."
        )
    if name in RESERVED_NAMES:
        raise ValueError(f"'{name}' is reserved by Openbase.")
    return name


def validate_local_port(port: int) -> int:
    if not 1 <= port <= 65535:
        raise ValueError("Local port must be between 1 and 65535.")
    return port


def validate_tailnet_port(port: int) -> int:
    if not DYNAMIC_PORT_MIN <= port <= DYNAMIC_PORT_MAX:
        raise ValueError(
            f"Tailnet ports must use the uncommon dynamic/private range "
            f"{DYNAMIC_PORT_MIN}-{DYNAMIC_PORT_MAX}."
        )
    return port


def validate_mode(value: str) -> str:
    mode = value.strip().lower()
    if mode not in {MODE_DYNAMIC, MODE_PORTLESS}:
        raise ValueError("Publication mode must be 'dynamic' or 'portless'.")
    return mode


def _registry_path() -> Path:
    return PUBLISHED_SERVICES_PATH


def _registry_lock_path() -> Path:
    return _registry_path().with_suffix(".lock")


@contextmanager
def registry_lock() -> Iterator[None]:
    depth = _REGISTRY_LOCK_DEPTH.get()
    token = _REGISTRY_LOCK_DEPTH.set(depth + 1)
    if depth:
        try:
            yield
        finally:
            _REGISTRY_LOCK_DEPTH.reset(token)
        return
    try:
        path = _registry_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "a+b") as handle:
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            path.chmod(0o600)
            flock(handle, LOCK_EX)
            try:
                yield
            finally:
                flock(handle, LOCK_UN)
    finally:
        _REGISTRY_LOCK_DEPTH.reset(token)


def load_registry() -> ServiceRegistry:
    path = _registry_path()
    if not path.is_file():
        return ServiceRegistry()
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("services", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise ValueError("Published service registry has an invalid services list.")
    services: list[PublishedService] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Published service registry contains an invalid entry.")
        mode = validate_mode(str(row.get("mode", MODE_DYNAMIC)))
        tailnet_port = int(row.get("tailnet_port", PORTLESS_TAILNET_PORT))
        if mode == MODE_DYNAMIC:
            tailnet_port = validate_tailnet_port(tailnet_port)
        elif tailnet_port != PORTLESS_TAILNET_PORT:
            raise ValueError("Portless services must use tailnet HTTP port 80.")
        services.append(
            PublishedService(
                name=validate_name(str(row["name"])),
                local_port=validate_local_port(int(row["local_port"])),
                tailnet_port=tailnet_port,
                proxy_port=validate_tailnet_port(int(row["proxy_port"])),
                persistent=bool(row.get("persistent", False)),
                pid=int(row["pid"]) if row.get("pid") is not None else None,
                mode=mode,
            )
        )
    applied_hash = payload.get("last_applied_serve_hash")
    if applied_hash is not None and not isinstance(applied_hash, str):
        raise ValueError("Published service registry has an invalid Serve hash.")
    return ServiceRegistry(tuple(services), applied_hash or None)


def load_services() -> list[PublishedService]:
    return list(load_registry().services)


def save_registry(registry: ServiceRegistry) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = {
        "version": 2,
        "services": [asdict(item) for item in registry.services],
        "last_applied_serve_hash": registry.last_applied_serve_hash,
    }
    with registry_lock():
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)


def save_services(
    services: list[PublishedService], *, last_applied_serve_hash: str | None = None
) -> None:
    if last_applied_serve_hash is None and _registry_path().is_file():
        last_applied_serve_hash = load_registry().last_applied_serve_hash
    save_registry(ServiceRegistry(tuple(services), last_applied_serve_hash))


def find_service(name: str) -> PublishedService | None:
    normalized = validate_name(name)
    return next((item for item in load_services() if item.name == normalized), None)


def published_serve_rules(*, persistent_only: bool = False) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    portless_added = False
    for item in load_services():
        if persistent_only and not item.persistent:
            continue
        if item.mode == MODE_PORTLESS:
            if portless_added:
                continue
            portless_added = True
        rules.append(item.serve_rule())
    return rules


def local_service_available(port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def choose_uncommon_port(used: set[int]) -> int:
    # Ask the kernel for an ephemeral port first, then fall back to a bounded
    # scan. Both paths remain inside IANA's dynamic/private range.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        candidate = int(listener.getsockname()[1])
    if candidate not in used and candidate >= DYNAMIC_PORT_MIN:
        return candidate
    for candidate in range(DYNAMIC_PORT_MIN, DYNAMIC_PORT_MAX + 1):
        if candidate not in used and _port_available(candidate):
            return candidate
    raise RuntimeError("No free dynamic/private port is available.")


def allocate_ports(tailnet_port: int | None = None) -> tuple[int, int]:
    services = load_services()
    used = {port for item in services for port in (item.tailnet_port, item.proxy_port)}
    if tailnet_port is not None:
        published = validate_tailnet_port(tailnet_port)
        if published in used:
            raise ValueError(f"Tailnet port {published} is already used by Openbase.")
    else:
        published = choose_uncommon_port(used)
    proxy = choose_uncommon_port(used | {published})
    return published, proxy


def allocate_portless_proxy() -> int:
    services = load_services()
    existing = next((item for item in services if item.mode == MODE_PORTLESS), None)
    if existing is not None:
        return existing.proxy_port
    used = {port for item in services for port in (item.tailnet_port, item.proxy_port)}
    return choose_uncommon_port(used)


def service_url(service: PublishedService) -> str:
    from openbase_coder_cli.services import tailscale_provider as tp

    status = tp.status_json()
    if status.get("error"):
        raise RuntimeError(str(status["error"]))
    self_status = status.get("Self") if isinstance(status.get("Self"), dict) else {}
    host = str(self_status.get("DNSName") or "").strip().rstrip(".")
    if not host:
        ips = self_status.get("TailscaleIPs")
        if isinstance(ips, list) and ips:
            host = str(ips[0]).strip()
    if not host:
        raise RuntimeError(
            "The active tailnet provider did not report a hostname or IP."
        )
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if service.mode == MODE_PORTLESS:
        return f"http://{host}{service.base_path}"
    return f"http://{host}:{service.tailnet_port}{service.base_path}"


def _runtime_python() -> str:
    package = stable_runtime_package()
    if package is not None and package.python_path.is_file():
        return str(package.python_path)
    return sys.executable


def _label(name: str) -> str:
    return f"{LAUNCHD_LABEL_PREFIX}.{name}"


def _wrapper_path(name: str) -> Path:
    return LAUNCHD_WRAPPER_DIR / f"published-service-{name}.sh"


def _plist_path(name: str) -> Path:
    return PLIST_DIR / f"{_label(name)}.plist"


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, check=False
    )


def _gateway_name(service: PublishedService) -> str:
    return PORTLESS_LAUNCHD_NAME if service.mode == MODE_PORTLESS else service.name


def _gateway_arguments(service: PublishedService) -> tuple[str, ...]:
    if service.mode == MODE_PORTLESS:
        return ("--dispatcher", "--port", str(service.proxy_port))
    return ("--name", service.name)


def install_launchd_service(service: PublishedService) -> None:
    if platform.system() != "Darwin":
        raise RuntimeError(
            "Persistent published services currently require macOS launchd."
        )
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHD_WRAPPER_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_DIR.mkdir(parents=True, exist_ok=True)
    gateway_name = _gateway_name(service)
    wrapper = _wrapper_path(gateway_name)
    command = " ".join(
        shlex.quote(part)
        for part in (
            _runtime_python(),
            "-m",
            "openbase_coder_cli.services.service_gateway",
            *_gateway_arguments(service),
        )
    )
    wrapper.write_text(f"#!/bin/sh\nexec {command}\n", encoding="utf-8")
    wrapper.chmod(0o700)
    plist = _plist_path(gateway_name)
    with plist.open("wb") as stream:
        plistlib.dump(
            {
                "Label": _label(gateway_name),
                "ProgramArguments": [str(wrapper)],
                "RunAtLoad": True,
                "KeepAlive": True,
                "ThrottleInterval": 5,
                "StandardOutPath": str(
                    DEFAULT_LOG_DIR / f"published-service-{gateway_name}.log"
                ),
                "StandardErrorPath": str(
                    DEFAULT_LOG_DIR / f"published-service-{gateway_name}.log"
                ),
            },
            stream,
        )
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{_label(gateway_name)}")
    result = _launchctl("bootstrap", domain, str(plist))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "launchctl bootstrap failed.")


def start_ephemeral_gateway(service: PublishedService) -> int:
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    gateway_name = _gateway_name(service)
    log = (DEFAULT_LOG_DIR / f"published-service-{gateway_name}.log").open("ab")
    try:
        process = subprocess.Popen(
            [
                _runtime_python(),
                "-m",
                "openbase_coder_cli.services.service_gateway",
                *_gateway_arguments(service),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()
    return process.pid


def gateway_healthy(service: PublishedService, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{service.proxy_port}{HEALTH_PATH}"
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=0.25).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    return False


def stop_gateway(service: PublishedService) -> None:
    gateway_name = _gateway_name(service)
    if service.persistent and platform.system() == "Darwin":
        domain = f"gui/{os.getuid()}"
        _launchctl("bootout", f"{domain}/{_label(gateway_name)}")
        for path in (_plist_path(gateway_name), _wrapper_path(gateway_name)):
            path.unlink(missing_ok=True)
        return
    if service.pid and _pid_is_gateway(service):
        try:
            os.kill(service.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _pid_is_gateway(service: PublishedService) -> bool:
    if not service.pid:
        return False
    result = subprocess.run(
        ["ps", "-p", str(service.pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    command = result.stdout.strip()
    if (
        result.returncode != 0
        or "openbase_coder_cli.services.service_gateway" not in command
    ):
        return False
    if service.mode == MODE_PORTLESS:
        return "--dispatcher" in command and f"--port {service.proxy_port}" in command
    return f"--name {service.name}" in command
