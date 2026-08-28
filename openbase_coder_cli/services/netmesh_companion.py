"""Provision the macOS netmesh VPN (Openbase VPN) from the CLI.

The netmesh VPN is a launchd root daemon (bundled ``tailscaled`` + the hardened
``NetmeshHelper``) carried by ``OpenbaseNetmeshCompanion.app``. Historically the
only thing that registered and connected it was the desktop Electron app, so a
CLI/``./scripts/setup`` developer who picked ``netmesh`` got the provider value
but no VPN. This module gives the CLI the same control path the Electron
manager (``desktop/electron/netmesh-companion.cjs``) has: locate/build the
companion, spawn it with a loopback IPC, register its root helper (SMAppService,
which needs a one-time GUI "background item" approval), and connect.

macOS-only. The daemon (and the VPN) outlive this process; we only run the
companion long enough to issue control operations.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_IPC_SECRET_HEADER = "X-Openbase-Companion-Secret"
_APP_NAME = "OpenbaseNetmeshCompanion.app"


class NetmeshCompanionError(RuntimeError):
    """The netmesh companion could not be provisioned."""


@dataclass(frozen=True)
class CompanionStatus:
    backend_state: str
    helper: str
    self_ip: str | None
    dns_name: str | None
    raw: dict

    @property
    def running(self) -> bool:
        return self.backend_state == "Running"

    @property
    def helper_enabled(self) -> bool:
        # The helper reports "enabled" once the SMAppService background item is
        # approved; "requires-approval"/"unknown" before that.
        return self.helper == "enabled"


def _companion_app_candidates(workspace_dir: Path | None) -> list[Path]:
    candidates: list[Path] = [
        # Shipping layout: nested in the installed desktop app.
        Path("/Applications/Openbase.app/Contents/Resources/" + _APP_NAME),
    ]
    if workspace_dir is not None:
        desktop = workspace_dir / "desktop"
        candidates += [
            desktop / "companion-build" / _APP_NAME,
            desktop
            / "netmesh-macos"
            / "DerivedData"
            / "Build"
            / "Products"
            / "Release"
            / _APP_NAME,
            desktop
            / "netmesh-macos"
            / "DerivedData"
            / "Build"
            / "Products"
            / "Debug"
            / _APP_NAME,
        ]
    return candidates


def _find_companion_app(workspace_dir: Path | None) -> Path | None:
    for candidate in _companion_app_candidates(workspace_dir):
        if candidate.is_dir():
            return candidate
    return None


def _missing_build_tools(workspace_dir: Path) -> list[str]:
    """Tools required to build the companion that aren't installed.

    This is the single source of truth for the netmesh (Openbase VPN) build
    prerequisites — the human docs point here rather than re-listing them.
    ``go`` is only needed when the pinned tailscale engine hasn't been staged
    yet (it's a gitignored ~56 MB build artifact).
    """
    import shutil

    missing: list[str] = []
    if shutil.which("node") is None:
        missing.append("node (https://nodejs.org — or `brew install node`)")
    if shutil.which("xcodegen") is None:
        missing.append("xcodegen (`brew install xcodegen`)")
    if shutil.which("xcodebuild") is None:
        missing.append(
            "Xcode (install from the App Store, then `xcodebuild -runFirstLaunch`)"
        )
    vendor = workspace_dir / "desktop" / "netmesh-macos" / "vendor" / "tailscale-bin"
    engine_staged = (vendor / "tailscaled").exists() and (vendor / "tailscale").exists()
    if not engine_staged and shutil.which("go") is None:
        missing.append(
            "go (`brew install go`) — builds the pinned tailscale engine; or "
            "stage prebuilt binaries into desktop/netmesh-macos/vendor/tailscale-bin/"
        )
    return missing


def _build_companion(workspace_dir: Path) -> Path:
    """Build the companion from the in-repo netmesh-macos project (dev)."""
    desktop = workspace_dir / "desktop"
    stage = desktop / "scripts" / "stage-netmesh-companion.mjs"
    if not stage.is_file():
        raise NetmeshCompanionError(
            f"Cannot build the netmesh companion: {stage} is missing."
        )
    missing = _missing_build_tools(workspace_dir)
    if missing:
        raise NetmeshCompanionError(
            "Building the Openbase VPN companion needs these tools first:\n  - "
            + "\n  - ".join(missing)
        )
    try:
        subprocess.run(  # noqa: S603,S607 - fixed argv, dev build
            ["node", str(stage)],
            cwd=str(desktop),
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise NetmeshCompanionError(
            f"Building the netmesh companion failed: {exc}"
        ) from exc
    built = desktop / "companion-build" / _APP_NAME
    if not built.is_dir():
        raise NetmeshCompanionError(
            f"Companion build reported success but {built} is missing."
        )
    return built


class NetmeshCompanion:
    """Locate/build and drive the macOS netmesh VPN companion over IPC."""

    def __init__(self, workspace_dir: str | Path | None = None) -> None:
        if sys.platform != "darwin":
            raise NetmeshCompanionError("The netmesh VPN is macOS-only.")
        self._workspace_dir = (
            Path(workspace_dir).expanduser() if workspace_dir else None
        )
        self._port = secrets.randbelow(21000) + 40000
        self._secret = secrets.token_hex(32)
        self._app_path: Path | None = None

    # -- location / build --------------------------------------------------

    def resolve_app(self, *, build_if_missing: bool = True) -> Path:
        found = _find_companion_app(self._workspace_dir)
        if found is not None:
            self._app_path = found
            return found
        if build_if_missing and self._workspace_dir is not None:
            self._app_path = _build_companion(self._workspace_dir)
            return self._app_path
        raise NetmeshCompanionError(
            f"{_APP_NAME} not found. Build it with "
            "desktop/scripts/stage-netmesh-companion.mjs, or install the "
            "Openbase desktop app."
        )

    # -- IPC ---------------------------------------------------------------

    def _request(
        self, method: str, path: str, body: dict | None = None, *, timeout: float = 15.0
    ) -> dict:
        payload = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}{path}",
            data=payload,
            method=method,
            headers={
                "Content-Type": "application/json",
                _IPC_SECRET_HEADER: self._secret,
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback
            return json.loads(resp.read().decode() or "{}")

    def _status_raw(self, *, timeout: float = 4.0) -> dict:
        return self._request("GET", "/status", timeout=timeout)

    def _parse_status(self, raw: dict) -> CompanionStatus:
        return CompanionStatus(
            backend_state=str(raw.get("backendState") or ""),
            helper=str(raw.get("helper") or "unknown"),
            self_ip=raw.get("selfIP") or None,
            dns_name=(
                str(raw.get("dnsName")).rstrip(".") if raw.get("dnsName") else None
            ),
            raw=raw,
        )

    # -- lifecycle ---------------------------------------------------------

    def ensure_running(self, *, build_if_missing: bool = True) -> CompanionStatus:
        try:
            return self._parse_status(self._status_raw())
        except (urllib.error.URLError, OSError, TimeoutError):
            pass  # not running yet — spawn it

        app = self.resolve_app(build_if_missing=build_if_missing)
        subprocess.run(  # noqa: S603,S607 - clean up any stale control process
            ["/usr/bin/pkill", "-f", "OpenbaseNetmeshCompanion"],
            check=False,
            capture_output=True,
        )
        subprocess.Popen(  # noqa: S603 - fixed argv
            [
                "/usr/bin/open",
                "-n",
                str(app),
                "--args",
                "--openbase-ipc-port",
                str(self._port),
                "--openbase-ipc-secret",
                self._secret,
            ],
        )
        deadline = time.monotonic() + 12.0
        last: str | None = None
        while time.monotonic() < deadline:
            time.sleep(0.4)
            try:
                return self._parse_status(self._status_raw())
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last = str(exc)
        raise NetmeshCompanionError(
            f"The netmesh companion did not become ready: {last or 'unknown error'}"
        )

    def register(self) -> CompanionStatus:
        return self._parse_status(self._request("POST", "/register"))

    def open_approval_settings(self) -> None:
        try:
            self._request("POST", "/open-approval-settings")
        except (urllib.error.URLError, OSError, TimeoutError):
            pass

    def connect(
        self, *, control_url: str, auth_key: str, hostname: str
    ) -> CompanionStatus:
        return self._parse_status(
            self._request(
                "POST",
                "/connect",
                {"controlURL": control_url, "authKey": auth_key, "hostname": hostname},
                timeout=45.0,
            )
        )

    def status(self) -> CompanionStatus:
        return self._parse_status(self._status_raw())

    def wait_for_helper_approval(self, *, timeout: float = 180.0) -> bool:
        """Poll until the SMAppService background item is approved."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self._parse_status(self._status_raw(timeout=3.0)).helper_enabled:
                    return True
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            time.sleep(2.0)
        return False
