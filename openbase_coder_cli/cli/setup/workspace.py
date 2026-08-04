"""Workspace phase: locate the dev workspace and install local runtime assets.

Openbase Coder has exactly two deployment modes:

- Development: the CLI is an editable install (``uv tool install -e``) or a
  workspace venv run against a developer's workspace checkout. Setup never
  clones or updates that checkout; it discovers where it already lives.
- Standalone: the CLI runs from a bundled runtime package (shipped with the
  desktop app or ``install.sh``) detected via ``openbase-coder-package.json``.
"""

from __future__ import annotations

import importlib.resources as importlib_resources
import json
import shlex
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from shutil import which
from urllib.parse import unquote, urlsplit

import click

from openbase_coder_cli.cli.node import run_workspace_package_command
from openbase_coder_cli.paths import (
    OPENBASE_BASE_DIR,
    OPENBASE_SOUNDS_DIR,
    STANDALONE_CURRENT_DIR,
    STANDALONE_RELEASES_DIR,
)
from openbase_coder_cli.runtime import RuntimePackage, current_runtime_package
from openbase_coder_cli.services.installation import InstallationConfig

BUNDLED_SOUNDS_PACKAGE = "openbase_coder_cli.resources.sounds"
BUNDLED_SOUND_FILES = ("deactivate.wav",)
THREAD_SYNC_EXCHANGE_DIR_NAME = "thread-sync"
THREAD_SYNC_MARKER_FILE_NAME = "syncthing-folder-openbase-thread-sync.txt"
THREAD_SYNC_STIGNORE_CONTENT = "#include .stglobalignore\n"
DEFAULT_SYNCTHING_GLOBAL_STIGNORE_CONTENT = "(?d).DS_Store\n"
CLI_PACKAGE_DIST_NAME = "openbase-coder"
UV_TOOL_SHEBANG_MARKER = "/uv/tools/openbase-coder/"


def resolve_dev_workspace_dir(explicit_dir: str | None) -> str:
    """Find the developer workspace checkout for dev-mode setup.

    Setup adapts to where the code already lives instead of cloning: an
    explicit ``--workspace-dir`` wins, then the workspace recorded by a prior
    install, then the checkout behind an editable CLI install.
    """
    if explicit_dir:
        workspace = Path(explicit_dir).expanduser()
        if not _looks_like_workspace(workspace):
            raise click.ClickException(
                f"{workspace} does not look like an Openbase Coder workspace "
                "checkout (expected a multi.json and a cli/ repo)."
            )
        return str(workspace)

    recorded = _recorded_workspace_dir()
    if recorded is not None:
        click.echo(f"Using workspace recorded by the current installation: {recorded}")
        return str(recorded)

    editable = _editable_install_workspace_dir()
    if editable is not None:
        click.echo(f"Using workspace behind the editable CLI install: {editable}")
        return str(editable)

    raise click.ClickException(
        "No Openbase Coder workspace found. Clone "
        "https://github.com/openbase-community/openbase-coder-workspace, run "
        "its scripts/setup, or pass --workspace-dir pointing at your checkout. "
        "End users should install the standalone package instead (see the "
        "desktop app or install.sh)."
    )


def _looks_like_workspace(path: Path) -> bool:
    return (path / "multi.json").is_file() and (path / "cli").is_dir()


def _recorded_workspace_dir() -> Path | None:
    if not InstallationConfig.exists():
        return None
    try:
        config = InstallationConfig.load()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not config.workspace_path:
        return None
    workspace = Path(config.workspace_path).expanduser()
    return workspace if _looks_like_workspace(workspace) else None


def _editable_install_workspace_dir() -> Path | None:
    try:
        dist = distribution(CLI_PACKAGE_DIST_NAME)
    except PackageNotFoundError:
        return None
    direct_url_text = dist.read_text("direct_url.json")
    if not direct_url_text:
        return None
    try:
        direct_url = json.loads(direct_url_text)
    except json.JSONDecodeError:
        return None
    if not direct_url.get("dir_info", {}).get("editable"):
        return None
    url = direct_url.get("url", "")
    if not url.startswith("file://"):
        return None
    cli_dir = Path(unquote(urlsplit(url).path))
    workspace = cli_dir.parent
    return workspace if _looks_like_workspace(workspace) else None


def _build_console(workspace_dir: str) -> None:
    console_dir = Path(workspace_dir) / "console"
    if not console_dir.is_dir():
        click.echo(f"Console directory not found at {console_dir}, skipping build.")
        return

    click.echo("Building console...")
    workspace_path = Path(workspace_dir)
    if not run_workspace_package_command(workspace_path, console_dir, "install"):
        return

    run_workspace_package_command(workspace_path, console_dir, "run", "build")
    click.echo("Console build complete.")


def _ensure_thread_sync_exchange_dir() -> None:
    """Create the Syncthing-backed cross-device Codex thread exchange folder."""
    exchange_dir = OPENBASE_BASE_DIR / THREAD_SYNC_EXCHANGE_DIR_NAME
    exchange_dir.mkdir(parents=True, exist_ok=True)

    marker_dir = exchange_dir / ".stfolder"
    marker_dir.mkdir(exist_ok=True)
    marker_path = marker_dir / THREAD_SYNC_MARKER_FILE_NAME
    if not marker_path.exists():
        marker_path.write_text(
            "Openbase Coder cross-device Codex thread snapshot exchange.\n",
            encoding="utf-8",
        )

    stignore_path = exchange_dir / ".stignore"
    if not stignore_path.exists():
        stignore_path.write_text(THREAD_SYNC_STIGNORE_CONTENT, encoding="utf-8")

    global_ignore_path = _syncthing_global_ignore_path()
    if not global_ignore_path.exists():
        global_ignore_path.parent.mkdir(parents=True, exist_ok=True)
        global_ignore_path.write_text(
            DEFAULT_SYNCTHING_GLOBAL_STIGNORE_CONTENT,
            encoding="utf-8",
        )

    stglobal_path = exchange_dir / ".stglobalignore"
    if stglobal_path.is_symlink():
        if stglobal_path.resolve() != global_ignore_path.resolve():
            stglobal_path.unlink()
            stglobal_path.symlink_to(global_ignore_path)
    elif not stglobal_path.exists():
        stglobal_path.symlink_to(global_ignore_path)

    click.echo(f"Prepared Codex thread sync exchange folder at {exchange_dir}")


def _ensure_bundled_sounds() -> None:
    """Install package-bundled sounds into Openbase's user sounds directory."""
    OPENBASE_SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    resources = importlib_resources.files(BUNDLED_SOUNDS_PACKAGE)
    for sound_name in BUNDLED_SOUND_FILES:
        resource = resources.joinpath(sound_name)
        with importlib_resources.as_file(resource) as source_path:
            if not source_path.is_file():
                raise click.ClickException(
                    f"Bundled sound resource not found: {sound_name}"
                )
            _copy_bundled_sound(
                source_path=source_path, target_path=OPENBASE_SOUNDS_DIR / sound_name
            )


def _copy_bundled_sound(*, source_path: Path, target_path: Path) -> None:
    if target_path.exists():
        if not target_path.is_file():
            click.echo(
                f"Bundled sound target already exists at {target_path}; "
                "leaving it unchanged."
            )
            return
        try:
            sound_matches = target_path.read_bytes() == source_path.read_bytes()
        except OSError:
            sound_matches = False
        if sound_matches:
            click.echo(f"Bundled sound already installed at {target_path}")
            return
        click.echo(
            f"Bundled sound already exists at {target_path} and differs from "
            "the package default; leaving it unchanged."
        )
        return

    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target_path)
    click.echo(f"Installed bundled sound at {target_path}")


def _syncthing_global_ignore_path() -> Path:
    return Path.home() / ".config" / "syncthing" / "global.stignore"


def _init_cli_workspace(
    workspace_dir: str,
    *,
    include_local_audio: bool = False,
) -> None:
    """Initialize the CLI checkout that now hosts the LiveKit worker."""
    cli_dir = Path(workspace_dir) / "cli"
    if not cli_dir.is_dir():
        click.echo("CLI directory not found, skipping worker init.")
        return

    uv_bin = which("uv")
    if not uv_bin:
        click.echo("'uv' not found on PATH, skipping CLI workspace init.")
        return

    click.echo("Initializing CLI workspace...")

    # Create venv and install dependencies
    click.echo("  Running uv sync...")
    sync_command = [uv_bin, "sync"]
    if include_local_audio:
        sync_command.extend(["--extra", "local-audio"])
    subprocess.run(sync_command, cwd=str(cli_dir), check=True)

    _download_livekit_model_files(
        [uv_bin, "run", "python"],
        cwd=str(cli_dir),
    )

    click.echo("CLI workspace initialization complete.")


def _init_standalone_runtime(runtime_package: RuntimePackage) -> None:
    """Prepare a bundled runtime: model files the package does not ship."""
    _download_livekit_model_files([str(runtime_package.python_path)])


def _download_livekit_model_files(
    python_command: list[str], cwd: str | None = None
) -> None:
    """Pre-fetch LiveKit agent model files (VAD, turn detector).

    Every installation needs these at voice-session time regardless of the
    selected audio provider; downloading during setup avoids a slow or
    offline-breaking first session.
    """
    click.echo("  Downloading LiveKit model files...")
    subprocess.run(
        [
            *python_command,
            "-m",
            "openbase_coder_cli.livekit_agent.livekit",
            "download-files",
        ],
        cwd=cwd,
        check=True,
    )


def _install_cli_shim(workspace_dir: str) -> None:
    """Install a stable `openbase-coder` user command.

    Never overwrites a uv-tool-managed script: an editable `uv tool install`
    already provides the command, and uv would fight over the file on its
    next upgrade.
    """
    user_bin = Path.home() / ".local" / "bin"
    user_bin.mkdir(parents=True, exist_ok=True)
    shim_path = user_bin / "openbase-coder"

    if _is_uv_tool_script(shim_path):
        click.echo(
            f"openbase-coder at {shim_path} is managed by `uv tool install`; "
            "leaving it unchanged."
        )
        return

    runtime_package = current_runtime_package()
    if runtime_package is not None:
        launcher = _stable_package_launcher(runtime_package)
        if launcher is None:
            click.echo(
                "Standalone package launcher not found; skipping CLI shim install."
            )
            return
        # The launcher locates its package root from its own path, so the shim
        # survives package upgrades when it points through current/.
        shim = f'#!/bin/sh\nexec {shlex.quote(str(launcher))} "$@"\n'
    else:
        # uv-workspace layout: the venv lives at the workspace root.
        candidates = (
            Path(workspace_dir) / ".venv" / "bin" / "openbase-coder",
            Path(workspace_dir) / "cli" / ".venv" / "bin" / "openbase-coder",
        )
        venv_cli = next((path for path in candidates if path.is_file()), None)
        if venv_cli is None:
            click.echo(
                f"Workspace venv binary not found at {candidates[0]}; "
                "skipping CLI shim install."
            )
            return
        shim = f'#!/bin/sh\nexec {shlex.quote(str(venv_cli))} "$@"\n'

    if shim_path.is_symlink():
        shim_path.unlink()
    shim_path.write_text(shim)
    shim_path.chmod(0o755)
    click.echo(f"Installed openbase-coder shim at {shim_path}")


def _is_uv_tool_script(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return False
    return first_line.startswith("#!") and UV_TOOL_SHEBANG_MARKER in first_line


def _stable_package_launcher(runtime_package: RuntimePackage) -> Path | None:
    """Prefer the current/ symlinked launcher so upgrades keep the shim fresh."""
    current_launcher = STANDALONE_CURRENT_DIR / "bin" / "openbase-coder"
    try:
        is_active_release = runtime_package.root.resolve().is_relative_to(
            STANDALONE_RELEASES_DIR.resolve()
        )
    except OSError:
        is_active_release = False
    if is_active_release and current_launcher.is_file():
        return current_launcher
    if runtime_package.openbase_coder_path.is_file():
        return runtime_package.openbase_coder_path
    return None
