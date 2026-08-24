"""
Doctor command — verify service health and security configuration.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import click

from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    CODING_BACKEND_ENV_KEY,
    DEFAULT_CODING_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    normalize_backend,
)
from openbase_coder_cli.claude_auth import claude_auth_status
from openbase_coder_cli.dispatcher_config import (
    selected_stt_provider_id,
    selected_tts_provider_id,
)
from openbase_coder_cli.paths import (
    CLAUDE_CONFIG_DIR,
    CLAUDE_STATE_PATH,
    CODEX_CONFIG_PATH,
    CODEX_HOME_DIR,
    DEFAULT_ENV_FILE_PATH,
    STANDALONE_RELEASES_DIR,
)
from openbase_coder_cli.platforms import is_windows
from openbase_coder_cli.runtime import stable_runtime_package
from openbase_coder_cli.services.definitions import SERVICES
from openbase_coder_cli.services.installation import InstallationConfig
from openbase_coder_cli.services.launchd import launchctl_status
from openbase_coder_cli.services.selection import configured_coding_backends
from openbase_coder_cli.services.tailscale_serve import tailscale_serve_health
from openbase_coder_cli.stt_providers import (
    LOCAL_MLX_WHISPER_STT_PROVIDER_ID,
    local_mlx_whisper_readiness,
)
from openbase_coder_cli.tts_providers import KOKORO_PROVIDER_ID, get_tts_provider

# Services that have authentication and may safely bind to 0.0.0.0
_AUTHENTICATED_PORTS: dict[int, str] = {
    7999: "django-cli (JWT auth)",
    7880: "livekit-server (API key auth)",
}

# Required env vars and known-insecure default values
_REQUIRED_ENV: list[tuple[str, list[str]]] = [
    ("OPENBASE_CODER_CLI_SECRET_KEY", []),
    ("LIVEKIT_API_KEY", ["devkey"]),
    ("LIVEKIT_API_SECRET", ["secret"]),
]

_LIVEKIT_CLIENT_ENV = ("LIVEKIT_CLIENT_API_KEY", "LIVEKIT_CLIENT_API_SECRET")


def _parse_env_file() -> dict[str, str]:
    """Read the .env file and return key-value pairs."""
    env: dict[str, str] = {}
    if not DEFAULT_ENV_FILE_PATH.is_file():
        return env
    for line in DEFAULT_ENV_FILE_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("\"'")
    return env


def _get_listening_sockets() -> list[tuple[str, int]]:
    """Return (bind_address, port) for all TCP LISTEN sockets.

    Uses lsof to query the system, falling back to ss when lsof is missing
    and to netstat on Windows, which has neither.
    """
    if is_windows():
        return _get_listening_sockets_netstat()
    try:
        result = subprocess.run(
            ["lsof", "-iTCP", "-sTCP:LISTEN", "-P", "-n"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return _get_listening_sockets_ss()
    seen: set[tuple[str, int]] = set()
    sockets: list[tuple[str, int]] = []
    for line in result.stdout.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 9:
            continue
        # NAME column is like "*:7880" or "127.0.0.1:2024"
        name = parts[8]
        if ":" not in name:
            continue
        host, _, port_str = name.rpartition(":")
        try:
            port = int(port_str)
        except ValueError:
            continue
        key = (host, port)
        if key not in seen:
            seen.add(key)
            sockets.append(key)
    return sockets


def _get_listening_sockets_netstat() -> list[tuple[str, int]]:
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    seen: set[tuple[str, int]] = set()
    sockets: list[tuple[str, int]] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        # "TCP  127.0.0.1:7999  0.0.0.0:0  LISTENING  27932"
        if len(parts) < 4 or parts[3].upper() != "LISTENING":
            continue
        host, separator, port_str = parts[1].rpartition(":")
        if not separator or not port_str.isdigit():
            continue
        # Keep the bracket-free form so the exposure checks below compare
        # against the same shapes lsof reports.
        key = (host.strip("[]"), int(port_str))
        if key not in seen:
            seen.add(key)
            sockets.append(key)
    return sockets


def _get_listening_sockets_ss() -> list[tuple[str, int]]:
    try:
        result = subprocess.run(
            ["ss", "-ltnH"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []
    seen: set[tuple[str, int]] = set()
    sockets: list[tuple[str, int]] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        # Local Address:Port column is like "0.0.0.0:7880" or "[::]:7880"
        name = parts[3]
        host, _, port_str = name.rpartition(":")
        try:
            port = int(port_str)
        except ValueError:
            continue
        key = (host, port)
        if key not in seen:
            seen.add(key)
            sockets.append(key)
    return sockets


def _check_livekit_client_credentials(env: dict[str, str], warn, ok) -> None:
    missing = [name for name in _LIVEKIT_CLIENT_ENV if not env.get(name)]
    if missing:
        warn(
            "LiveKit client token credentials missing "
            f"({', '.join(missing)}): run 'openbase-coder setup' and restart services"
        )
        return

    reused = []
    if env.get("LIVEKIT_CLIENT_API_KEY") == env.get("LIVEKIT_API_KEY"):
        reused.append("LIVEKIT_CLIENT_API_KEY")
    if env.get("LIVEKIT_CLIENT_API_SECRET") == env.get("LIVEKIT_API_SECRET"):
        reused.append("LIVEKIT_CLIENT_API_SECRET")
    if reused:
        warn(
            "LiveKit client token credentials reuse local server credentials "
            f"({', '.join(reused)}): run 'openbase-coder setup' and restart services"
        )
        return

    ok("LiveKit client token credentials: set and separate from server credentials")


def _selected_backend(env: dict[str, str]) -> str:
    raw_value = env.get(CODING_BACKEND_ENV_KEY) or DEFAULT_CODING_BACKEND
    try:
        return normalize_backend(raw_value)
    except ValueError:
        return DEFAULT_CODING_BACKEND


def _check_installation_config(ok, warn, fail) -> None:
    if not InstallationConfig.exists():
        fail("installation.json missing — run 'openbase-coder setup'")
        return

    ok("installation.json found")
    try:
        config = InstallationConfig.load()
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        fail(f"installation.json could not be read: {exc}")
        return

    if config.standalone:
        ok("standalone runtime mode enabled")
        package = stable_runtime_package()
        if package is None:
            fail(
                "standalone install, but this CLI is not running from a "
                "runtime package — reinstall or run the packaged "
                "openbase-coder"
            )
            return
        _check_path(str(package.root), "standalone package", ok, fail, directory=True)
        _check_path(str(package.python_path), "bundled Python", ok, fail)
        _check_path(
            str(package.livekit_server_path), "bundled LiveKit server", ok, fail
        )
        _check_path(
            str(package.console_build_dir),
            "bundled console build",
            ok,
            fail,
            directory=True,
            child="index.html",
        )
        if config.workspace_path:
            warn(
                "dev workspace is still configured in standalone mode; "
                "rerun setup from the standalone CLI to clear it"
            )
        else:
            ok("no workspace checkout required at runtime")
        return

    if config.workspace_path:
        ok("development workspace runtime mode enabled")
        workspace = Path(config.workspace_path)
        _check_workspace_asset_roots(workspace, ok, warn)
        _check_console_build_freshness(workspace, ok, warn)
    else:
        warn("non-standalone install has no workspace_path configured")


def _check_workspace_asset_roots(workspace: Path, ok, warn) -> None:
    missing = [
        name
        for name in ("instructions", "skills", "console")
        if not (workspace / name).is_dir()
    ]
    if missing:
        warn(
            f"workspace is missing asset roots ({', '.join(missing)}) — "
            "instruction/skill symlinks and console serving depend on them; "
            "restore the checkout or re-run setup with --workspace-dir"
        )
    else:
        ok("workspace asset roots present (instructions, skills, console)")


def _check_console_build_freshness(workspace: Path, ok, warn) -> None:
    """Dev serves console/dist directly; a stale build silently misrepresents
    current frontend code, so surface staleness as a diagnosis."""
    dist = workspace / "console" / "dist"
    if not (dist / "index.html").is_file():
        warn("console/dist has no build — run 'pnpm run build' in console/")
        return
    built = _newest_mtime(dist)
    sources = [workspace / "console" / "src", workspace / "coder-react" / "src"]
    newest_source = max(
        (_newest_mtime(source) for source in sources if source.is_dir()),
        default=0.0,
    )
    if newest_source > built:
        warn(
            "console/dist is older than the console sources — run "
            "'pnpm run build' in console/ (the server serves dist directly)"
        )
    else:
        ok("console build is up to date with console sources")


def _newest_mtime(root: Path) -> float:
    import os

    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames if name not in {"node_modules", ".git"}
        ]
        for filename in filenames:
            try:
                newest = max(newest, (Path(dirpath) / filename).stat().st_mtime)
            except OSError:
                continue
    return newest


def _check_path(
    value: str,
    label: str,
    ok,
    fail,
    *,
    directory: bool = False,
    child: str | None = None,
) -> None:
    if not value:
        fail(f"{label}: not configured")
        return

    path = Path(value).expanduser()
    exists = path.is_dir() if directory else path.is_file()
    if child and exists:
        exists = (path / child).is_file()
    if exists:
        ok(f"{label}: {path}")
    else:
        fail(f"{label}: missing at {path}")


def _check_agent_auth(env: dict[str, str], ok, warn, fail, action=None) -> None:
    action = action or fail
    backend = _selected_backend(env)
    ok(f"coding backend selected: {backend}")

    codex_auth = CODEX_HOME_DIR / "auth.json"
    if backend == CODEX_BACKEND:
        if codex_auth.is_file():
            ok("Codex auth: logged in")
        else:
            action("Codex auth missing: run 'codex login'")

    if backend == OPENBASE_CLOUD_BACKEND:
        from openbase_coder_cli.services.onboarding import cloud_login_status

        login = cloud_login_status()
        if login["status"] == "logged_in":
            if login["validated"]:
                ok("Openbase Cloud auth: logged in")
            else:
                warn(f"Openbase Cloud auth: {login['detail']}")
        elif login["status"] == "login_expired":
            action(
                "Openbase Cloud login expired or was revoked: "
                "run 'openbase-coder login' again"
            )
        else:
            action("Openbase Cloud auth missing: run 'openbase-coder login'")

    if backend == CLAUDE_CODE_BACKEND:
        status = claude_auth_status()
        if status.logged_in:
            ok("Claude Code auth: logged in")
        else:
            detail = f" ({status.raw_output})" if status.raw_output else ""
            action(f"Claude Code auth missing: run 'claude auth login'{detail}")


def _read_super_agents_command_codex(path: Path) -> str | None:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entry = data.get("mcp_servers", {}).get("super-agents")
    if isinstance(entry, dict):
        return entry.get("command")
    return None


def _read_super_agents_command_claude(path: Path) -> str | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data.get("mcpServers", {}).get("super-agents")
    if isinstance(entry, dict):
        return entry.get("command")
    return None


def _check_super_agents_mcp_registrations(ok, warn, fail) -> None:
    """Verify the super-agents MCP command resolves in every agent home.

    Release rotation deletes old versioned package dirs, so a command path
    pinned under releases/ is one update away from dangling even when it
    still exists today.
    """
    configs = (
        (
            "Codex config",
            CODEX_CONFIG_PATH,
            _read_super_agents_command_codex,
        ),
        (
            "Claude config",
            CLAUDE_STATE_PATH,
            _read_super_agents_command_claude,
        ),
    )
    seen: dict[Path, str] = {}
    for label, path, reader in configs:
        if not path.is_file():
            warn(f"{label}: missing at {path}; run 'openbase-coder setup'")
            continue
        resolved = path.resolve()
        if resolved in seen:
            ok(f"{label}: linked to {seen[resolved]}")
            continue
        seen[resolved] = label
        try:
            command = reader(path)
        except (OSError, ValueError) as exc:
            warn(f"{label}: unreadable at {path}: {exc}")
            continue
        if not command:
            warn(
                f"{label}: super-agents MCP not registered; run 'openbase-coder setup'"
            )
            continue
        command_path = Path(command)
        if not command_path.is_file():
            fail(
                f"{label}: super-agents MCP command missing at {command_path}; "
                "run 'openbase-coder setup'"
            )
        elif command_path.is_relative_to(STANDALONE_RELEASES_DIR):
            warn(
                f"{label}: super-agents MCP command pinned to versioned release "
                f"{command_path}; it dangles after the next update — "
                "run 'openbase-coder setup'"
            )
        else:
            ok(f"{label}: super-agents MCP command at {command_path}")


def _check_agent_home_skills(ok, warn, fail) -> None:
    for label, skills_dir in (
        ("Codex home skills", CODEX_HOME_DIR / "skills"),
        ("Claude config skills", CLAUDE_CONFIG_DIR / "skills"),
    ):
        if not skills_dir.is_dir():
            warn(f"{label}: missing at {skills_dir}; run 'openbase-coder setup'")
            continue
        entries = [
            entry
            for entry in sorted(skills_dir.iterdir())
            if not entry.name.startswith(".")
        ]
        if not entries:
            warn(f"{label}: no skills linked; run 'openbase-coder setup'")
            continue
        dangling = [
            entry.name for entry in entries if entry.is_symlink() and not entry.exists()
        ]
        pinned = [
            entry.name
            for entry in entries
            if entry.is_symlink()
            and entry.exists()
            and entry.readlink().is_relative_to(STANDALONE_RELEASES_DIR)
        ]
        if dangling:
            fail(
                f"{label}: dangling symlinks ({', '.join(dangling)}); "
                "run 'openbase-coder setup'"
            )
        if pinned:
            warn(
                f"{label}: pinned to versioned release ({', '.join(pinned)}); "
                "they dangle after the next update — run 'openbase-coder setup'"
            )
        if not dangling and not pinned:
            ok(f"{label}: {len(entries)} entries resolve")


def _check_audio_readiness(ok, warn) -> None:
    tts_provider = selected_tts_provider_id()
    stt_provider = selected_stt_provider_id()
    ok(f"TTS provider selected: {tts_provider}")
    ok(f"STT provider selected: {stt_provider}")

    if tts_provider == KOKORO_PROVIDER_ID:
        status = get_tts_provider(KOKORO_PROVIDER_ID).readiness()
        if status.ready:
            ok(
                "Kokoro local audio: ready "
                f"({status.cached_files}/{status.required_files} files cached)"
            )
        else:
            detail = f": {status.detail}" if status.detail else ""
            warn(
                "Kokoro local audio not ready "
                f"({status.cached_files}/{status.required_files} files cached)"
                f"{detail}; run 'openbase-coder setup --audio-provider local'"
            )

    if stt_provider == LOCAL_MLX_WHISPER_STT_PROVIDER_ID:
        status = local_mlx_whisper_readiness()
        if status.ready:
            ok(f"Local MLX Whisper: ready ({status.model})")
        else:
            detail = f": {status.detail}" if status.detail else ""
            warn(
                "Local MLX Whisper not ready "
                f"({status.model}){detail}; "
                "run 'openbase-coder setup --audio-provider local'"
            )


_VERSIONS_WARN_BYTES = 2 * 1024**3  # 2 GiB
_GIT_IGNORE_PATTERN = "(?d).git"


def _syncthing_process_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "syncthing"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _stignore_content_with_includes(path: Path) -> str:
    """A .stignore's text plus one level of ``#include`` targets.

    Syncthing resolves includes relative to the .stignore's directory; the
    VCS patterns typically live in an included .stglobalignore, so checking
    only the top-level file false-positives on correctly configured setups.
    """
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    parts = [content]
    for line in content.splitlines():
        if not line.startswith("#include "):
            continue
        include_path = path.parent / line[len("#include ") :].strip()
        try:
            parts.append(include_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def _check_code_sync(ok, warn, fail) -> None:
    from openbase_coder_cli.code_sync.ignores import STIGNORE_FILENAME
    from openbase_coder_cli.code_sync.manager import versions_usage_bytes
    from openbase_coder_cli.services.registry import find_service
    from openbase_coder_cli.sync_config import code_sync_enabled, sync_folders

    # Guard against user-managed Syncthing setups that still sync `.git`
    # (torn git state; the reason code-sync excludes VCS metadata).
    user_stignore = Path.home() / "Projects" / ".stignore"
    if user_stignore.is_file() and _syncthing_process_running():
        if _GIT_IGNORE_PATTERN not in _stignore_content_with_includes(user_stignore):
            warn(
                f"a Syncthing instance is running and {user_stignore} does not "
                f"ignore '{_GIT_IGNORE_PATTERN}': syncing .git corrupts repos; "
                "add the VCS patterns or migrate to 'openbase-coder sync'"
            )
        else:
            ok("user-managed Syncthing ignores .git")

    try:
        enabled = code_sync_enabled()
    except ValueError as exc:
        fail(f"sync-config.json unreadable: {exc}")
        return
    if not enabled:
        ok("code sync: disabled")
        return

    # The managed .stignore is only rewritten on enable/apply; if one goes
    # missing or loses the VCS block, Syncthing quietly starts syncing .git.
    for folder in sync_folders():
        stignore = folder.absolute_path() / STIGNORE_FILENAME
        if _GIT_IGNORE_PATTERN in _stignore_content_with_includes(stignore):
            continue
        fail(
            f"managed sync folder '{folder.relpath}' has no .git ignore in "
            f"{stignore}: run 'openbase-coder sync enable' to regenerate it, "
            "or .git corruption can recur"
        )

    info = launchctl_status(find_service("code-sync"))
    if not info.get("installed"):
        fail("code-sync service: not installed (run 'openbase-coder sync enable')")
    elif info.get("pid"):
        ok(f"code-sync service: running (pid {info['pid']})")
    else:
        fail(
            "code-sync service: not running "
            f"(last exit: {info.get('last_exit_code', 'unknown')})"
        )

    usage = versions_usage_bytes()
    if usage > _VERSIONS_WARN_BYTES:
        warn(
            f"sync version history uses {usage / 1024**3:.1f} GiB; purge it "
            "with 'POST /api/sync/versions/purge/' or from the console"
        )
    else:
        ok(f"sync version history: {usage / 1024**2:.0f} MiB")


@click.command()
def doctor() -> None:
    """Check service health and security configuration."""
    ok_count = 0
    warn_count = 0
    fail_count = 0
    action_count = 0

    def ok(msg: str) -> None:
        nonlocal ok_count
        ok_count += 1
        click.echo(click.style("  OK  ", fg="green") + msg)

    def warn(msg: str) -> None:
        nonlocal warn_count
        warn_count += 1
        click.echo(click.style("  WARN ", fg="yellow") + msg)

    def fail(msg: str) -> None:
        nonlocal fail_count
        fail_count += 1
        click.echo(click.style("  FAIL ", fg="red") + msg)

    def action(msg: str) -> None:
        nonlocal action_count
        action_count += 1
        click.echo(click.style("  SETUP", fg="cyan") + " " + msg)

    # --- Installation ---
    click.echo()
    click.echo(click.style("Installation", bold=True))
    _check_installation_config(ok, warn, fail)

    # --- Service health ---
    click.echo()
    click.echo(click.style("Service Health", bold=True))
    coding_backends = configured_coding_backends()
    backends_label = "/".join(coding_backends)
    for svc in SERVICES:
        if not any(svc.supports_backend(backend) for backend in coding_backends):
            ok(f"{svc.name}: not used ({backends_label} backend)")
            continue
        info = launchctl_status(svc)
        required = getattr(svc, "install_by_default", True)
        if not info["installed"]:
            if required:
                fail(f"{svc.name}: not installed")
            else:
                ok(f"{svc.name}: optional (not installed)")
        elif info["pid"]:
            ok(f"{svc.name}: running (pid {info['pid']})")
        else:
            exit_code = info.get("last_exit_code", "unknown")
            if required:
                fail(f"{svc.name}: not running (last exit: {exit_code})")
            else:
                ok(f"{svc.name}: optional (not running, last exit: {exit_code})")

    # --- Bind address security ---
    click.echo()
    click.echo(click.style("Network Security", bold=True))
    sockets = _get_listening_sockets()

    for port, label in _AUTHENTICATED_PORTS.items():
        listeners = [(h, p) for h, p in sockets if p == port]
        if not listeners:
            warn(f"port {port} ({label}): not listening")
        else:
            for host, _ in listeners:
                if host in ("*", "0.0.0.0", "[::]"):
                    ok(f"port {port} ({label}): bound to {host} (auth enabled)")
                else:
                    ok(f"port {port} ({label}): bound to {host}")

    # --- Tailscale Serve ---
    click.echo()
    click.echo(click.style("Tailscale Serve", bold=True))
    serve_health = tailscale_serve_health()
    if not serve_health.tailscale_available:
        action("tailscale: not found on PATH")
    elif not serve_health.tailscale_running:
        action(f"tailscale: not running ({serve_health.error or 'unknown error'})")
    else:
        ok(f"tailscale: running for {serve_health.host or 'unknown host'}")

    if serve_health.openbase_configured:
        ok("Openbase API Serve route: :18080 -> http://127.0.0.1:7999")
    else:
        action(
            "Openbase API Serve route missing: run "
            "tailscale serve --bg --http=18080 http://127.0.0.1:7999"
        )

    if serve_health.livekit_configured:
        ok("LiveKit Serve route: :7880 -> tcp://127.0.0.1:7880")
    else:
        action(
            "LiveKit Serve route missing: run "
            "tailscale serve --bg --tcp=7880 tcp://127.0.0.1:7880"
        )

    if serve_health.openbase_reachable:
        ok(f"external Openbase health check passed at {serve_health.openbase_url}")
    else:
        detail = f": {serve_health.error}" if serve_health.error else ""
        action(f"external Openbase health check failed{detail}")

    # --- Credentials ---
    click.echo()
    click.echo(click.style("Credentials", bold=True))
    env = _parse_env_file()

    if not DEFAULT_ENV_FILE_PATH.is_file():
        fail(f".env file not found at {DEFAULT_ENV_FILE_PATH}")
    else:
        ok(f".env file exists at {DEFAULT_ENV_FILE_PATH}")

    for var_name, insecure_values in _REQUIRED_ENV:
        value = env.get(var_name, "")
        if not value:
            fail(f"{var_name}: not set")
        elif value in insecure_values:
            fail(f"{var_name}: using insecure default value '{value}'")
        else:
            ok(f"{var_name}: set")

    _check_livekit_client_credentials(env, warn, ok)

    # --- Agent auth ---
    click.echo()
    click.echo(click.style("Agent Auth", bold=True))
    _check_agent_auth(env, ok, warn, fail, action)

    # --- Agent homes ---
    click.echo()
    click.echo(click.style("Agent Homes", bold=True))
    _check_super_agents_mcp_registrations(ok, warn, fail)
    _check_agent_home_skills(ok, warn, fail)

    # --- Audio ---
    click.echo()
    click.echo(click.style("Audio", bold=True))
    _check_audio_readiness(ok, warn)

    # --- Code Sync ---
    click.echo()
    click.echo(click.style("Code Sync", bold=True))
    _check_code_sync(ok, warn, fail)

    # --- Summary ---
    click.echo()
    total = ok_count + warn_count + fail_count + action_count
    summary = f"{ok_count}/{total} checks passed"
    if fail_count:
        summary += f", {fail_count} failed"
    if warn_count:
        summary += f", {warn_count} warnings"
    if action_count:
        summary += f", {action_count} setup actions"

    if fail_count:
        click.echo(click.style(summary, fg="red", bold=True))
    elif warn_count or action_count:
        click.echo(click.style(summary, fg="yellow", bold=True))
    else:
        click.echo(click.style(summary, fg="green", bold=True))
    click.echo()
