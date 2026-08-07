"""Locate or install coding-backend CLI binaries (codex, claude).

Neither backend CLI ships inside the standalone runtime package: both
self-update and release far more often than Openbase Coder. Instead setup
installs the one the selected backend needs on demand — codex from its GitHub
release binaries into ``~/.openbase/bin``, claude via Anthropic's official
native installer.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from shutil import which

import click

from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)
from openbase_coder_cli.paths import OPENBASE_BIN_DIR

CODEX_LATEST_RELEASE_URL = "https://api.github.com/repos/openai/codex/releases/latest"
CLAUDE_INSTALLER_URL = "https://claude.ai/install.sh"
BACKEND_BINARY_NAMES = {
    CODEX_BACKEND: "codex",
    OPENBASE_CLOUD_BACKEND: "claude",
    OPENBASE_CLOUD_CODEX_BACKEND: "codex",
    CLAUDE_CODE_BACKEND: "claude",
}


def backend_binary_name(coding_backend: str) -> str | None:
    return BACKEND_BINARY_NAMES.get(coding_backend)


def nvm_binary_candidates(name: str) -> list[Path]:
    candidates: list[Path] = []

    nvm_bin = os.environ.get("NVM_BIN")
    if nvm_bin:
        candidates.append(Path(nvm_bin) / name)

    nvm_dir = Path(os.environ.get("NVM_DIR") or Path.home() / ".nvm")
    candidates.extend(sorted(nvm_dir.glob(f"versions/node/*/bin/{name}"), reverse=True))

    return candidates


def backend_binary_candidates(name: str) -> list[Path]:
    """Places a backend CLI may live, beyond PATH lookup."""
    return [
        OPENBASE_BIN_DIR / name,
        Path.home() / ".local" / "bin" / name,
        *nvm_binary_candidates(name),
    ]


def find_backend_binary(name: str) -> Path | None:
    for candidate in backend_binary_candidates(name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    found = which(name)
    return Path(found) if found else None


def ensure_backend_binary(coding_backend: str) -> Path | None:
    """Find the selected backend's CLI, installing it if missing.

    Best-effort: on failure this reports instructions and returns None so
    setup can continue; service installation surfaces the hard error if the
    binary is genuinely required and still absent.
    """
    name = backend_binary_name(coding_backend)
    if name is None:
        return None

    existing = find_backend_binary(name)
    if existing is not None:
        click.echo(f"Found {name} CLI at {existing}")
        return existing

    click.echo(f"'{name}' CLI not found; installing...")
    try:
        if name == "codex":
            return _install_codex()
        return _install_claude()
    except (
        OSError,
        subprocess.CalledProcessError,
        urllib.error.URLError,
        tarfile.TarError,
        json.JSONDecodeError,
        RuntimeError,
    ) as exc:
        click.echo(
            click.style(
                f"  WARN  Could not install the {name} CLI automatically: {exc}",
                fg="yellow",
            )
        )
        click.echo(_manual_install_hint(name))
        return None


def _manual_install_hint(name: str) -> str:
    if name == "codex":
        return (
            "        Install it manually (e.g. `npm install -g @openai/codex` or a "
            "release binary from https://github.com/openai/codex/releases), then "
            "run `openbase-coder services install`."
        )
    return (
        "        Install it manually with `curl -fsSL https://claude.ai/install.sh "
        "| bash`, then run `openbase-coder services install`."
    )


def _codex_release_target() -> str:
    machine = platform.machine().lower()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64"}.get(machine)
    if arch is None:
        raise RuntimeError(f"Unsupported architecture for codex install: {machine}")
    system = platform.system()
    if system == "Darwin":
        return f"{arch}-apple-darwin"
    if system == "Linux":
        return f"{arch}-unknown-linux-musl"
    raise RuntimeError(f"Unsupported platform for codex install: {system}")


def _install_codex() -> Path:
    target = _codex_release_target()
    asset_name = f"codex-{target}.tar.gz"
    with urllib.request.urlopen(CODEX_LATEST_RELEASE_URL, timeout=30) as response:
        release = json.load(response)
    asset_url = next(
        (
            asset.get("browser_download_url")
            for asset in release.get("assets", [])
            if asset.get("name") == asset_name
        ),
        None,
    )
    if not asset_url:
        raise RuntimeError(
            f"No {asset_name} asset in codex release {release.get('tag_name')}"
        )

    OPENBASE_BIN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        archive_path = tmp_dir / asset_name
        urllib.request.urlretrieve(asset_url, archive_path)
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(tmp_dir, filter="data")
        binary = _find_extracted_binary(tmp_dir, "codex")
        if binary is None:
            raise RuntimeError(f"No codex binary found inside {asset_name}")
        installed = OPENBASE_BIN_DIR / "codex"
        shutil.copy2(binary, installed)
        installed.chmod(0o755)

    click.echo(f"Installed codex CLI at {installed}")
    return installed


def refresh_openbase_bin_codex() -> bool:
    """Re-download codex when we installed it (it cannot self-update there).

    Returns True when a refresh happened; False when codex is user-managed or
    absent. Used by self-update so the pinned release binary doesn't go stale.
    """
    installed = OPENBASE_BIN_DIR / "codex"
    if not installed.is_file():
        return False
    _install_codex()
    return True


def _find_extracted_binary(root: Path, name: str) -> Path | None:
    for candidate in sorted(root.rglob(f"{name}*")):
        if candidate.is_file() and not candidate.name.endswith((".tar.gz", ".zst")):
            return candidate
    return None


def _install_claude() -> Path:
    subprocess.run(
        ["/bin/bash", "-c", f"curl -fsSL {CLAUDE_INSTALLER_URL} | bash"],
        check=True,
    )
    installed = find_backend_binary("claude")
    if installed is None:
        raise RuntimeError(
            "The Claude Code installer completed but no 'claude' binary was found."
        )
    click.echo(f"Installed claude CLI at {installed}")
    return installed
