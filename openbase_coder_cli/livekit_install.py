"""Install the pinned livekit-server for development installs.

Standalone packages bundle the pinned engine by construction; dev installs
historically ran whatever Homebrew had. This downloads the exact pin from
``livekit_version.py`` into ``~/.openbase/bin`` so both pathways run the same
engine: Linux from the official LiveKit release binaries, macOS by extracting
the bundled binary from the latest Openbase Coder standalone package
(upstream publishes no darwin builds). Homebrew/PATH remains a last-resort
fallback and still trips the version-skew health warning when it diverges.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import click

from openbase_coder_cli.livekit_version import LIVEKIT_SERVER_PINNED_VERSION
from openbase_coder_cli.paths import OPENBASE_BIN_DIR
from openbase_coder_cli.platforms import is_windows
from openbase_coder_cli.self_update import RELEASE_REPO

LIVEKIT_RELEASE_URL_TEMPLATE = (
    "https://github.com/livekit/livekit/releases/download/"
    "v{version}/livekit_{version}_linux_{arch}.tar.gz"
)
LIVEKIT_WINDOWS_RELEASE_URL_TEMPLATE = (
    "https://github.com/livekit/livekit/releases/download/"
    "v{version}/livekit_{version}_windows_{arch}.zip"
)
PACKAGE_ASSET_URL_TEMPLATE = (
    f"https://github.com/{RELEASE_REPO}/releases/latest/download/"
    "openbase-coder-package-{target}.tar.gz"
)


def installed_livekit_server_path() -> Path:
    name = "livekit-server.exe" if is_windows() else "livekit-server"
    return OPENBASE_BIN_DIR / name


def ensure_pinned_livekit_server() -> Path | None:
    """Install the pinned livekit-server into ~/.openbase/bin for dev.

    Returns the installed path, or None when the pin could not be installed
    (the service resolver then falls back to PATH/Homebrew and the skew
    health warning keeps the divergence visible).
    """
    pin = LIVEKIT_SERVER_PINNED_VERSION
    installed = installed_livekit_server_path()
    if installed.is_file() and _binary_version(installed) == pin:
        return installed

    system = platform.system()
    try:
        if system == "Linux":
            return _install_binary(_download_from_livekit_release(pin), pin)
        if system == "Darwin":
            return _install_binary(_download_from_openbase_package(pin), pin)
        if system == "Windows":
            return _install_binary(_download_from_livekit_windows_release(pin), pin)
    except (
        OSError,
        urllib.error.URLError,
        tarfile.TarError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
        RuntimeError,
    ) as exc:
        click.echo(
            click.style(
                f"  WARN  Could not install pinned livekit-server {pin}: {exc}. "
                "Falling back to PATH/Homebrew; version skew will be reported "
                "by health warnings.",
                fg="yellow",
            )
        )
        return None
    return None


def _install_binary(staged: Path, pin: str) -> Path:
    version = _binary_version(staged)
    if version != pin:
        raise RuntimeError(
            f"downloaded livekit-server reports {version or 'no version'}, "
            f"expected the pinned {pin} (bump livekit_version.py and cut a "
            "release before dev installs can adopt a new engine)"
        )
    installed = installed_livekit_server_path()
    installed.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite the installed binary in place: the service may be
    # running it, and rewriting a signed executable's pages invalidates the
    # kernel's cached code signature — later execs die with SIGKILL (Code
    # Signature Invalid). Stage next to the target and rename into place so
    # the new binary gets a fresh vnode.
    staging = installed.with_name(f"{installed.name}.new.{os.getpid()}")
    shutil.copy2(staged, staging)
    staging.chmod(0o755)
    staging.replace(installed)
    click.echo(f"Installed pinned livekit-server {pin} at {installed}")
    return installed


def _download_from_livekit_release(pin: str) -> Path:
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}.get(
        platform.machine().lower()
    )
    if arch is None:
        raise RuntimeError(f"unsupported architecture {platform.machine()}")
    url = LIVEKIT_RELEASE_URL_TEMPLATE.format(version=pin, arch=arch)
    return _extract_livekit_server(url)


def _download_from_livekit_windows_release(pin: str) -> Path:
    # Unlike darwin, upstream does publish Windows builds, so take them
    # directly instead of unpacking an Openbase standalone package.
    arch = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "amd64": "amd64",
        "x86_64": "amd64",
    }.get(platform.machine().lower())
    if arch is None:
        raise RuntimeError(f"unsupported architecture {platform.machine()}")
    url = LIVEKIT_WINDOWS_RELEASE_URL_TEMPLATE.format(version=pin, arch=arch)
    staging = Path(tempfile.mkdtemp(prefix="openbase-livekit-"))
    archive_path = staging / "archive.zip"
    urllib.request.urlretrieve(url, archive_path)  # noqa: S310 — pinned https URL
    with zipfile.ZipFile(archive_path) as archive:
        member = next(
            (
                item
                for item in archive.namelist()
                if not item.endswith("/")
                and Path(item).name.lower() == "livekit-server.exe"
            ),
            None,
        )
        if member is None:
            raise RuntimeError(f"livekit-server.exe missing from {url}")
        archive.extract(member, staging)
    return staging / member


def _download_from_openbase_package(pin: str) -> Path:
    machine = platform.machine().lower()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64"}.get(machine)
    if arch is None:
        raise RuntimeError(f"unsupported architecture {platform.machine()}")
    url = PACKAGE_ASSET_URL_TEMPLATE.format(target=f"{arch}-apple-darwin")
    return _extract_livekit_server(url)


def _extract_livekit_server(url: str) -> Path:
    staging = Path(tempfile.mkdtemp(prefix="openbase-livekit-"))
    archive_path = staging / "archive.tar.gz"
    urllib.request.urlretrieve(url, archive_path)  # noqa: S310 — pinned https URL
    with tarfile.open(archive_path, "r:gz") as archive:
        member = next(
            (
                item
                for item in archive
                if item.isfile()
                and (
                    item.name == "livekit-server"
                    or item.name.endswith("/livekit-server")
                    or item.name.endswith("bin/livekit-server")
                )
            ),
            None,
        )
        if member is None:
            raise RuntimeError(f"no livekit-server binary inside {url}")
        member.name = "livekit-server"
        archive.extract(member, staging, filter="data")
    staged = staging / "livekit-server"
    staged.chmod(0o755)
    return staged


def _binary_version(binary: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+\.\d+\.\d+)", result.stdout + result.stderr)
    return match.group(1) if match else None
