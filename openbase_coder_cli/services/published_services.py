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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

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


@dataclass(frozen=True)
class PublishedService:
    name: str
    local_port: int
    tailnet_port: int
    proxy_port: int
    persistent: bool = False
    pid: int | None = None

    @property
    def target(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}"

    def serve_rule(self) -> dict[str, Any]:
        return {"proto": "http", "port": self.tailnet_port, "target": self.target}


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


def _registry_path() -> Path:
    return PUBLISHED_SERVICES_PATH


def load_services() -> list[PublishedService]:
    path = _registry_path()
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("services", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise ValueError("Published service registry has an invalid services list.")
    services: list[PublishedService] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Published service registry contains an invalid entry.")
        services.append(
            PublishedService(
                name=validate_name(str(row["name"])),
                local_port=validate_local_port(int(row["local_port"])),
                tailnet_port=validate_tailnet_port(int(row["tailnet_port"])),
                proxy_port=validate_tailnet_port(int(row["proxy_port"])),
                persistent=bool(row.get("persistent", False)),
                pid=int(row["pid"]) if row.get("pid") is not None else None,
            )
        )
    return services


def save_services(services: list[PublishedService]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {"version": 1, "services": [asdict(item) for item in services]}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def find_service(name: str) -> PublishedService | None:
    normalized = validate_name(name)
    return next((item for item in load_services() if item.name == normalized), None)


def published_serve_rules(*, persistent_only: bool = False) -> list[dict[str, Any]]:
    return [
        item.serve_rule()
        for item in load_services()
        if item.persistent or not persistent_only
    ]


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
    return f"http://{host}:{service.tailnet_port}/{service.name}/"


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


def install_launchd_service(service: PublishedService) -> None:
    if platform.system() != "Darwin":
        raise RuntimeError(
            "Persistent published services currently require macOS launchd."
        )
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHD_WRAPPER_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_DIR.mkdir(parents=True, exist_ok=True)
    wrapper = _wrapper_path(service.name)
    command = " ".join(
        shlex.quote(part)
        for part in (
            _runtime_python(),
            "-m",
            "openbase_coder_cli.services.service_gateway",
            "--name",
            service.name,
        )
    )
    wrapper.write_text(f"#!/bin/sh\nexec {command}\n", encoding="utf-8")
    wrapper.chmod(0o700)
    plist = _plist_path(service.name)
    with plist.open("wb") as stream:
        plistlib.dump(
            {
                "Label": _label(service.name),
                "ProgramArguments": [str(wrapper)],
                "RunAtLoad": True,
                "KeepAlive": True,
                "ThrottleInterval": 5,
                "StandardOutPath": str(
                    DEFAULT_LOG_DIR / f"published-service-{service.name}.log"
                ),
                "StandardErrorPath": str(
                    DEFAULT_LOG_DIR / f"published-service-{service.name}.log"
                ),
            },
            stream,
        )
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{_label(service.name)}")
    result = _launchctl("bootstrap", domain, str(plist))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "launchctl bootstrap failed.")


def start_ephemeral_gateway(service: PublishedService) -> int:
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = (DEFAULT_LOG_DIR / f"published-service-{service.name}.log").open("ab")
    try:
        process = subprocess.Popen(
            [
                _runtime_python(),
                "-m",
                "openbase_coder_cli.services.service_gateway",
                "--name",
                service.name,
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
    if service.persistent and platform.system() == "Darwin":
        domain = f"gui/{os.getuid()}"
        _launchctl("bootout", f"{domain}/{_label(service.name)}")
        for path in (_plist_path(service.name), _wrapper_path(service.name)):
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
    return (
        result.returncode == 0
        and "openbase_coder_cli.services.service_gateway" in command
        and f"--name {service.name}" in command
    )


def apply_route(service: PublishedService) -> None:
    from openbase_coder_cli.services import tailscale_provider as tp

    if tp.is_netmesh_tsnet():
        raise RuntimeError(
            "Openbase Direct cannot publish arbitrary host services. "
            "Switch this computer to Openbase VPN first."
        )
    if tp.is_netmesh() and not tp.netmesh_uses_stock_tailscale():
        from openbase_coder_cli.services.tailscale_serve import openbase_serve_rules

        tp.apply_serve([*openbase_serve_rules(), *published_serve_rules()])
    else:
        tp.apply_serve([service.serve_rule()])


def remove_route(service: PublishedService) -> None:
    from openbase_coder_cli.services import tailscale_provider as tp

    if tp.is_netmesh() and not tp.netmesh_uses_stock_tailscale():
        from openbase_coder_cli.services.tailscale_serve import openbase_serve_rules

        tp.apply_serve([*openbase_serve_rules(), *published_serve_rules()])
    else:
        tp.remove_serve("http", service.tailnet_port)
