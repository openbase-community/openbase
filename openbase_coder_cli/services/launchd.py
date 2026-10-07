from __future__ import annotations

import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from string import Formatter

import click

from openbase_coder_cli.backend_binaries import backend_binary_candidates
from openbase_coder_cli.env_file import selected_backend_from_env_file
from openbase_coder_cli.paths import (
    DEFAULT_ENV_FILE_PATH,
    DEFAULT_LOG_DIR,
    LAUNCHD_DOMAIN,
    LAUNCHD_WRAPPER_DIR,
    OPENBASE_BASE_DIR,
    OPENBASE_BIN_DIR,
    PLIST_DIR,
    TASK_SCHEDULER_DIR,
)
from openbase_coder_cli.runtime import stable_runtime_package
from openbase_coder_cli.services import process_utils
from openbase_coder_cli.services.definitions import (
    RETIRED_SERVICE_NAMES,
    SERVICES,
    ServiceDefinition,
    default_services,
    retired_service_stub,
)
from openbase_coder_cli.services.installation import InstallationConfig


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _resolve_binary(name: str, homebrew_fallback: str | None = None) -> str:
    path = shutil.which(name)
    if path:
        return path
    py_dir = Path(sys.executable).parent
    for candidate in [py_dir / name, py_dir / f"{name}.exe"]:
        if candidate.is_file():
            return str(candidate)
    fallbacks: list[Path] = []
    if homebrew_fallback:
        fallbacks.append(Path(homebrew_fallback))
    fallbacks.append(Path.home() / ".local" / "bin" / name)
    for fallback in fallbacks:
        if fallback.is_file():
            return str(fallback)
    raise click.ClickException(
        f"Could not find '{name}' on PATH. Please install it first."
    )


def _workspace_binary_candidates(config: InstallationConfig, name: str) -> list[Path]:
    if not config.workspace_path:
        return []
    workspace = Path(config.workspace_path)
    return [
        workspace / ".venv" / "bin" / name,
        workspace / ".venv" / "Scripts" / name,
        workspace / ".venv" / "Scripts" / f"{name}.exe",
        workspace / "cli" / ".venv" / "bin" / name,
        workspace / "cli" / ".venv" / "Scripts" / name,
        workspace / "cli" / ".venv" / "Scripts" / f"{name}.exe",
        workspace / "agent" / ".venv" / "bin" / name,
        workspace / "agent" / ".venv" / "Scripts" / name,
        workspace / "agent" / ".venv" / "Scripts" / f"{name}.exe",
    ]


def _resolve_binary_with_preferred_paths(
    name: str,
    preferred_paths: list[Path],
    homebrew_fallback: str | None = None,
) -> str:
    for path in preferred_paths:
        if path.is_file() and (sys.platform == "win32" or os.access(path, os.X_OK)):
            return str(path)
    return _resolve_binary(name, homebrew_fallback)


def _resolve_syncthing() -> str:
    from openbase_coder_cli.code_sync.syncthing import resolve_syncthing_binary

    return resolve_syncthing_binary()


def _resolve_livekit_server(package) -> str:
    if package is not None:
        return _resolve_binary_with_preferred_paths(
            "livekit-server",
            [package.livekit_server_path],
            "/opt/homebrew/bin/livekit-server",
        )

    from openbase_coder_cli.livekit_install import (
        fallback_livekit_server_path,
        installed_livekit_server_path,
        livekit_binary_matches_pin,
    )

    downloaded = installed_livekit_server_path()
    if (
        downloaded.is_file()
        and os.access(downloaded, os.X_OK)
        and livekit_binary_matches_pin(downloaded)
    ):
        return str(downloaded)
    fallback = fallback_livekit_server_path()
    if fallback is not None:
        return str(fallback)
    return _resolve_binary("livekit-server", "/opt/homebrew/bin/livekit-server")


def _runtime_workdir(config: InstallationConfig) -> str:
    runtime_package = stable_runtime_package()
    if runtime_package is not None:
        return str(runtime_package.root)
    return config.workspace_path or str(OPENBASE_BASE_DIR)


def _resolve_service_python(package) -> str:
    if package is not None and package.python_path.is_file():
        return str(package.python_path)
    return sys.executable


def _binary_resolvers(config: InstallationConfig) -> dict[str, Callable[[], str]]:
    # Standalone binaries are derived from the runtime package at generation
    # time (routed through the stable current/ alias) — never persisted, so a
    # self-update flip can't leave services pointing at a pruned release.
    package = stable_runtime_package()
    return {
        "uv": lambda: _resolve_binary_with_preferred_paths(
            "uv",
            _workspace_binary_candidates(config, "uv"),
            "/opt/homebrew/bin/uv",
        ),
        "codex": lambda: _resolve_binary_with_preferred_paths(
            "codex",
            backend_binary_candidates("codex"),
        ),
        "claude": lambda: _resolve_binary_with_preferred_paths(
            "claude",
            backend_binary_candidates("claude"),
        ),
        "livekit": lambda: _resolve_livekit_server(package),
        "python": lambda: _resolve_service_python(package),
        "syncthing": _resolve_syncthing,
        "openbase_coder": lambda: _resolve_binary_with_preferred_paths(
            "openbase-coder",
            [
                *([package.openbase_coder_path] if package is not None else []),
                *_workspace_binary_candidates(config, "openbase-coder"),
            ],
        ),
        "openbase_syncd": lambda: _resolve_binary_with_preferred_paths(
            "openbase-syncd",
            [
                *(
                    [Path(os.environ["OPENBASE_SYNCD_BIN"])]
                    if os.environ.get("OPENBASE_SYNCD_BIN")
                    else []
                ),
                OPENBASE_BIN_DIR / "openbase-syncd",
                *_workspace_binary_candidates(config, "openbase-syncd"),
            ],
        ),
        "tunneld": lambda: _resolve_binary_with_preferred_paths(
            "openbase-tunneld",
            [
                *(
                    [Path(os.environ["OPENBASE_TUNNELD_BIN"])]
                    if os.environ.get("OPENBASE_TUNNELD_BIN")
                    else []
                ),
                OPENBASE_BIN_DIR / "openbase-tunneld",
                *_workspace_binary_candidates(config, "openbase-tunneld"),
            ],
        ),
        "runtime_workdir": lambda: _runtime_workdir(config),
    }


def _service_template_keys(services: Iterable[ServiceDefinition]) -> set[str]:
    # ``command_template`` is now a plain runner key (see services/runners.py)
    # and never contains ``{...}`` fields — only ``workdir_template`` still
    # needs scanning (e.g. "{runtime_workdir}").
    keys: set[str] = set()
    for svc in services:
        for _text, field, _spec, _conv in Formatter().parse(svc.workdir_template):
            if field:
                keys.add(field)
    return keys


def _resolve_binaries(
    config: InstallationConfig,
    services: Iterable[ServiceDefinition] | None = None,
) -> dict[str, str]:
    """Resolve only the binaries the given services actually reference.

    Resolution raises for binaries that cannot be found, so limiting it to the
    services being installed keeps optional backends (e.g. codex when Claude
    Code is selected) from failing installs on machines without them.
    """
    if services is None:
        services = SERVICES
    resolvers = _binary_resolvers(config)
    # "python" is always needed: every wrapper now execs the runner module
    # through it, regardless of what workdir_template references.
    keys = _service_template_keys(services) | {"python"}
    return {key: resolvers[key]() for key in sorted(keys) if key in resolvers}


def _selected_backend(config: InstallationConfig) -> str:
    env_path = (
        Path(config.env_file).expanduser() if config.env_file else DEFAULT_ENV_FILE_PATH
    )
    return selected_backend_from_env_file(env_path)


def _uid() -> int:
    return os.getuid()


def _service_label(svc: ServiceDefinition) -> str:
    return f"{LAUNCHD_DOMAIN}.{svc.name}"


def _wrapper_path(svc: ServiceDefinition) -> Path:
    return LAUNCHD_WRAPPER_DIR / f"{svc.name}.sh"


def _plist_path(svc: ServiceDefinition) -> Path:
    return PLIST_DIR / f"{_service_label(svc)}.plist"


def _log_path(svc: ServiceDefinition) -> Path:
    return DEFAULT_LOG_DIR / f"{svc.name}.log"


def _truncate_log_file(path: Path, max_lines: int = 5000) -> None:
    if not path.exists():
        return

    lines = path.read_text(errors="replace").splitlines()
    trimmed = "\n".join(lines[-max_lines:])
    if trimmed:
        trimmed += "\n"

    with path.open("r+", encoding="utf-8", errors="replace") as handle:
        handle.seek(0)
        handle.write(trimmed)
        handle.truncate()


def _truncate_existing_logs(svc: ServiceDefinition) -> None:
    _truncate_log_file(_log_path(svc))


def _matches_cleanup_signature(svc: ServiceDefinition, pid: int) -> bool:
    if not svc.cleanup_command_substrings:
        return True

    command = process_utils.process_cmdline(pid)
    return all(token in command for token in svc.cleanup_command_substrings)


def _cleanup_candidate_pids(svc: ServiceDefinition) -> set[int]:
    candidates: set[int] = set()
    for port in svc.cleanup_ports:
        for pid in process_utils.listening_pids(port):
            if _matches_cleanup_signature(svc, pid):
                candidates.add(pid)
    return candidates


def _cleanup_lingering_processes(
    svc: ServiceDefinition, keep: frozenset[int] = frozenset()
) -> None:
    """Kill leftover listeners on the service's ports, sparing ``keep`` pids."""
    lingering_pids = _cleanup_candidate_pids(svc) - keep

    if not lingering_pids:
        return

    for pid in lingering_pids:
        process_utils.terminate(pid)

    time.sleep(1)

    stubborn_pids = _cleanup_candidate_pids(svc) - keep

    for pid in stubborn_pids:
        process_utils.terminate(pid, force=True)


def _cleanup_service_endpoint(svc: ServiceDefinition) -> None:
    if (
        svc.name not in ("codex-app-server", "codex-app-server-dispatcher")
        or _is_windows()
    ):
        return
    from openbase_coder_cli.codex_control_plane import (
        cleanup_stale_codex_app_server_socket,
        dispatcher_codex_app_server_endpoint,
        managed_codex_app_server_endpoint,
    )
    from openbase_coder_cli.paths import CODEX_HOME_DIR

    endpoint_env = {"CODEX_HOME": str(CODEX_HOME_DIR)}
    endpoint = (
        managed_codex_app_server_endpoint(endpoint_env, platform=sys.platform)
        if svc.name == "codex-app-server"
        else dispatcher_codex_app_server_endpoint(endpoint_env, platform=sys.platform)
    )
    cleanup_stale_codex_app_server_socket(endpoint)


def _ensure_launchd_paths() -> None:
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHD_WRAPPER_DIR.mkdir(parents=True, exist_ok=True)
    if _is_macos():
        PLIST_DIR.mkdir(parents=True, exist_ok=True)
    elif _is_windows():
        TASK_SCHEDULER_DIR.mkdir(parents=True, exist_ok=True)
    else:
        from openbase_coder_cli.paths import SYSTEMD_UNIT_DIR

        SYSTEMD_UNIT_DIR.mkdir(parents=True, exist_ok=True)


def _write_service_files(
    svc: ServiceDefinition,
    config: InstallationConfig,
    binaries: dict[str, str],
) -> bool:
    """Generate the service's wrapper and unit files.

    Returns True when the job must be re-registered for the change to take
    effect: on macOS only a changed plist (launchd execs the wrapper fresh on
    every start, so wrapper edits are picked up by an in-place restart); always
    on Windows/Linux, where re-registration is harmless.
    """
    if not _is_macos() and _is_windows():
        # Windows has no shell, so it skips the bash wrapper entirely —
        # Task Scheduler execs python -m ...runners directly.
        from openbase_coder_cli.services.windows import generate_task_xml

        generate_task_xml(svc, config, binaries["python"])
        return True

    _write_if_changed(
        _wrapper_path(svc), render_wrapper(svc, config, binaries), mode=0o755
    )
    if _is_macos():
        return _write_if_changed(_plist_path(svc), render_plist(svc, config))

    from openbase_coder_cli.services.systemd import generate_unit

    generate_unit(svc, config)
    return True


def _prepare_service_start(svc: ServiceDefinition) -> None:
    _truncate_existing_logs(svc)
    _cleanup_lingering_processes(svc)


def render_wrapper(
    svc: ServiceDefinition,
    config: InstallationConfig,
    binaries: dict[str, str],
) -> str:
    # In standalone mode there is no workspace checkout; fall back so
    # workdirs never render as an empty string.
    workspace = config.workspace_path or _runtime_workdir(config)
    env_file = config.env_file
    data_dir = str(OPENBASE_BASE_DIR)

    # The python path lands in shell command position (e.g. the bundled CLI
    # under "/Applications/Openbase Coder.app" contains a space) — quote it.
    python_bin = shlex.quote(binaries["python"])
    cmd = (
        f"exec {python_bin} -m openbase_coder_cli.services.runners "
        f"{svc.command_template}"
    )
    workdir = svc.workdir_template.format(
        workspace=workspace, data_dir=data_dir, **binaries
    )

    return textwrap.dedent(f"""\
        #!/bin/bash
        # Auto-generated wrapper for {svc.name}

        cd "{workdir}"

        if [ -f "{env_file}" ]; then
            set -a
            source "{env_file}"
            set +a
        fi

        export PATH="$HOME/.openbase/bin:$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

        {cmd}
    """)


def generate_wrapper(
    svc: ServiceDefinition,
    config: InstallationConfig,
    binaries: dict[str, str],
) -> Path:
    wrapper = _wrapper_path(svc)
    _write_if_changed(wrapper, render_wrapper(svc, config, binaries), mode=0o755)
    return wrapper


DESKTOP_APP_BUNDLE_ID = "tech.openbase.coder.desktop"
_DESKTOP_APP_NAMES = ("Openbase.app", "Openbase Coder.app")
_SYSTEM_APPLICATIONS_DIR = Path("/Applications")


def _installed_desktop_bundle_id() -> str | None:
    """Bundle id of an installed Openbase desktop app, or None when absent.

    Dev builds carry their own bundle id, so read it from the bundle rather
    than assuming the release id.
    """
    roots = (_SYSTEM_APPLICATIONS_DIR, Path.home() / "Applications")
    for root in roots:
        for name in _DESKTOP_APP_NAMES:
            info = root / name / "Contents" / "Info.plist"
            if not info.is_file():
                continue
            try:
                bundle_id = plistlib.loads(info.read_bytes()).get("CFBundleIdentifier")
            except (OSError, plistlib.InvalidFileException, ValueError):
                continue
            if isinstance(bundle_id, str) and bundle_id:
                return bundle_id
    return None


def _associated_bundle_id(config: InstallationConfig) -> str | None:
    installed = _installed_desktop_bundle_id()
    if installed:
        return installed
    if config.standalone:
        return DESKTOP_APP_BUNDLE_ID
    return None


def render_plist(svc: ServiceDefinition, config: InstallationConfig) -> str:
    label = _service_label(svc)
    wrapper = _wrapper_path(svc)
    workdir = svc.workdir_template.format(
        workspace=config.workspace_path or _runtime_workdir(config),
        data_dir=str(OPENBASE_BASE_DIR),
        runtime_workdir=_runtime_workdir(config),
    )
    log_dir = DEFAULT_LOG_DIR
    associated_bundle = ""
    bundle_id = _associated_bundle_id(config)
    if bundle_id:
        # Legacy LaunchAgents otherwise appear as anonymous `<name>.sh` items
        # from an unknown developer in macOS background-item notifications and
        # System Settings. Associate them with the installed desktop app so
        # macOS presents them under the Openbase identity and groups them.
        associated_bundle = textwrap.dedent(f"""\
            <key>AssociatedBundleIdentifiers</key>
            <array>
                <string>{bundle_id}</string>
            </array>
        """)

    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{label}</string>
{textwrap.indent(associated_bundle, "            ")}
            <key>ProgramArguments</key>
            <array>
                <string>{wrapper}</string>
            </array>
            <key>WorkingDirectory</key>
            <string>{workdir}</string>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <{str(svc.keep_alive).lower()}/>
            <key>ThrottleInterval</key>
            <integer>5</integer>
            <key>StandardOutPath</key>
            <string>{log_dir}/{svc.name}.log</string>
            <key>StandardErrorPath</key>
            <string>{log_dir}/{svc.name}.log</string>
        </dict>
        </plist>
    """)


def generate_plist(svc: ServiceDefinition, config: InstallationConfig) -> Path:
    plist = _plist_path(svc)
    _write_if_changed(plist, render_plist(svc, config))
    return plist


def _write_if_changed(path: Path, text: str, mode: int | None = None) -> bool:
    """Write ``text`` to ``path`` only when its content differs.

    Returns True when the file was created or rewritten. Leaving an unchanged
    file untouched matters on macOS: Background Task Management treats a
    modified LaunchAgent plist as a new background item and notifies the
    user ("Background Items Added") every time.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        changed = path.read_text() != text
    except (OSError, UnicodeDecodeError):
        changed = True
    if changed:
        path.write_text(text)
    if mode is not None and (
        not path.exists() or (path.stat().st_mode & 0o777) != mode
    ):
        path.chmod(mode)
    return changed


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def launchctl_bootstrap(svc: ServiceDefinition) -> None:
    if not _is_macos():
        if _is_windows():
            from openbase_coder_cli.services.windows import windows_bootstrap

            windows_bootstrap(svc)
            return
        from openbase_coder_cli.services.systemd import systemd_bootstrap

        systemd_bootstrap(svc)
        return

    label = _service_label(svc)
    plist = _plist_path(svc)
    domain = f"gui/{_uid()}"
    _prepare_service_start(svc)
    _launchctl("enable", f"{domain}/{label}", check=False)
    for attempt in range(4):
        # Bootout on each attempt in case a prior bootstrap partially registered
        _launchctl("bootout", f"{domain}/{label}", check=False)
        time.sleep(0.5 * (attempt + 1))
        result = _launchctl("bootstrap", domain, str(plist), check=False)
        if result.returncode == 0:
            # An intermittent macOS state has been observed where bootstrap
            # succeeds but the job stays loaded without a PID, despite
            # RunAtLoad and KeepAlive (mechanism unconfirmed). Kickstart
            # without -k is a no-op for a running job and safely ensures the
            # newly registered job is asked to run.
            _launchctl("kickstart", f"{domain}/{label}", check=False)
            return
    raise click.ClickException(f"Failed to bootstrap {label}: {result.stderr.strip()}")


def launchctl_bootout(svc: ServiceDefinition) -> bool:
    if not _is_macos():
        if _is_windows():
            from openbase_coder_cli.services.windows import windows_bootout

            return windows_bootout(svc)
        from openbase_coder_cli.services.systemd import systemd_bootout

        return systemd_bootout(svc)

    label = _service_label(svc)
    result = _launchctl("bootout", f"gui/{_uid()}/{label}", check=False)
    _cleanup_lingering_processes(svc)
    _cleanup_service_endpoint(svc)
    return result.returncode == 0


def launchctl_kickstart(svc: ServiceDefinition) -> bool:
    if not _is_macos():
        if _is_windows():
            from openbase_coder_cli.services.windows import windows_kickstart

            return windows_kickstart(svc)
        from openbase_coder_cli.services.systemd import systemd_kickstart

        return systemd_kickstart(svc)

    label = _service_label(svc)
    _prepare_service_start(svc)
    result = _launchctl("kickstart", "-k", f"gui/{_uid()}/{label}", check=False)
    return result.returncode == 0


# How long an in-place restart waits for the old process to exit on SIGTERM
# before falling back to launchd's forced kickstart.
RESTART_EXIT_TIMEOUT_SECONDS = 15.0


def _job_pid(svc: ServiceDefinition) -> int | None:
    pid = launchctl_status(svc).get("pid")
    try:
        return int(pid) if pid else None
    except (TypeError, ValueError):
        return None


def launchctl_restart(svc: ServiceDefinition) -> bool:
    """Restart an already-loaded launchd job without unloading it.

    Bootout + bootstrap re-registers the job with Background Task Management,
    which makes macOS post a "Background Items Added" notification on every
    restart. Keeping the job loaded and only replacing its process avoids
    that: SIGTERM the running instance (launchd's KeepAlive respawns it), wait
    for the old pid to go away, and kickstart in case the job is a one-shot
    or still throttled.

    Returns False when the job is not loaded (the caller must bootstrap) or
    on a non-macOS platform (where re-registration is harmless).
    """
    if not _is_macos():
        return False

    status = launchctl_status(svc)
    if not status.get("installed"):
        return False

    label = _service_label(svc)
    target = f"gui/{_uid()}/{label}"
    _truncate_existing_logs(svc)

    old_pid = _job_pid(svc)
    if old_pid is not None:
        _launchctl("kill", "SIGTERM", target, check=False)
        pid = process_utils.wait_for_pid_change(
            lambda: _job_pid(svc), old_pid, timeout=RESTART_EXIT_TIMEOUT_SECONDS
        )
        if pid == old_pid:
            # The process ignored SIGTERM; let launchd kill and relaunch it.
            _launchctl("kickstart", "-k", target, check=False)

    new_pid = _job_pid(svc)
    keep = process_utils.process_tree_pids(new_pid) if new_pid else set()
    _cleanup_lingering_processes(svc, keep=frozenset(keep))
    # No-op when launchd already respawned the job; starts it otherwise.
    _launchctl("kickstart", target, check=False)
    return True


def launchctl_kill(svc: ServiceDefinition) -> bool:
    if not _is_macos():
        if _is_windows():
            from openbase_coder_cli.services.windows import windows_kill

            return windows_kill(svc)
        from openbase_coder_cli.services.systemd import systemd_kill

        return systemd_kill(svc)

    label = _service_label(svc)
    result = _launchctl("kill", "SIGTERM", f"gui/{_uid()}/{label}", check=False)
    _cleanup_lingering_processes(svc)
    return result.returncode == 0


# Set to "external" when something other than launchd/systemd supervises the
# generated wrappers (e.g. the Docker entrypoint). The supervisor maintains
# <data_dir>/run/<name>.pid files: written on service start, removed on exit.
EXTERNAL_SUPERVISOR_ENV = "OPENBASE_CODER_SERVICE_SUPERVISOR"
EXTERNAL_SUPERVISOR_RUN_DIR = OPENBASE_BASE_DIR / "run"


def _external_supervisor() -> bool:
    return os.environ.get(EXTERNAL_SUPERVISOR_ENV, "").lower() == "external"


def _external_supervisor_status(svc: ServiceDefinition) -> dict:
    if not _wrapper_path(svc).is_file():
        return {"installed": False}
    pid: int | None = None
    try:
        pid = int((EXTERNAL_SUPERVISOR_RUN_DIR / f"{svc.name}.pid").read_text().strip())
    except (OSError, ValueError):
        pid = None
    if pid is not None:
        try:
            os.kill(pid, 0)
        except OSError:
            pid = None
    if not svc.install_by_default and pid is None:
        # Wrapper regeneration writes files for optional services (code-sync,
        # cloud heartbeat) regardless of whether their feature is on; under
        # an external supervisor "installed" means actually supervised, so a
        # disabled feature doesn't warn as an unexpectedly installed service.
        return {"installed": False}
    return {"installed": True, "pid": str(pid) if pid else None}


def launchctl_status(svc: ServiceDefinition) -> dict:
    if _external_supervisor():
        return _external_supervisor_status(svc)
    if not _is_macos():
        if _is_windows():
            from openbase_coder_cli.services.windows import windows_status

            return windows_status(svc)
        from openbase_coder_cli.services.systemd import systemd_status

        return systemd_status(svc)

    label = _service_label(svc)
    result = _launchctl("print", f"gui/{_uid()}/{label}", check=False)
    if result.returncode != 0:
        return {"installed": False}

    info = result.stdout
    pid = None
    last_exit = None
    for line in info.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            pid = line.split("=")[1].strip()
        if "last exit code" in line:
            last_exit = line.split("=")[-1].strip()

    return {
        "installed": True,
        "pid": pid if pid and pid != "0" else None,
        "last_exit_code": last_exit,
    }


def install_all_services(config: InstallationConfig) -> None:
    _ensure_launchd_paths()
    coding_backend = _selected_backend(config)
    services = default_services(coding_backend)
    binaries = _resolve_binaries(config, services)

    for svc in default_services():
        if svc in services:
            continue
        if remove_service(svc):
            click.echo(
                f"  Removed {svc.name} (not used by the {coding_backend} backend)."
            )

    # Upgrades must not strand processes for services that no longer exist.
    for name in RETIRED_SERVICE_NAMES:
        if remove_service(retired_service_stub(name)):
            click.echo(f"  Removed retired service {name}.")

    for svc in services:
        click.echo(f"  Installing {svc.name}...")
        reload_required = _write_service_files(svc, config, binaries)
        verb = _activate_service(svc, reload_required)
        click.echo(f"    {verb} {_service_label(svc)}")

    click.echo()
    click.echo("All services installed and started.")
    click.echo(f"Logs: {DEFAULT_LOG_DIR}/")


def remove_service(svc: ServiceDefinition) -> bool:
    """Unload a service and delete its generated files. True if any existed."""
    existed = False
    plist = _plist_path(svc)
    wrapper = _wrapper_path(svc)
    if launchctl_status(svc).get("installed"):
        launchctl_bootout(svc)
        existed = True
    _cleanup_service_endpoint(svc)
    for path in (plist, wrapper):
        if path.exists():
            path.unlink()
            existed = True
    return existed


def _activate_service(svc: ServiceDefinition, reload_required: bool) -> str:
    """Start or restart ``svc`` after its files were (re)generated.

    A loaded job whose plist did not change is restarted in place, so routine
    restarts never re-register the background item with macOS. Only a changed
    plist (or an unloaded job) goes through bootout + bootstrap, since that is
    the only way launchd re-reads the plist. Returns the verb for user output.
    """
    if not reload_required and launchctl_restart(svc):
        return "Restarted"
    launchctl_bootstrap(svc)
    return "Loaded"


def install_service(config: InstallationConfig, svc: ServiceDefinition) -> None:
    """Install ``svc`` if needed and (re)start it."""
    _ensure_launchd_paths()
    binaries = _resolve_binaries(config, [svc])
    reload_required = _write_service_files(svc, config, binaries)
    _activate_service(svc, reload_required)


def regenerate_service(config: InstallationConfig, svc: ServiceDefinition) -> None:
    _ensure_launchd_paths()
    binaries = _resolve_binaries(config, [svc])
    _write_service_files(svc, config, binaries)


def regenerate_all_services(config: InstallationConfig) -> None:
    _ensure_launchd_paths()
    coding_backend = _selected_backend(config)

    for svc in SERVICES:
        if not svc.supports_backend(coding_backend):
            click.echo(f"  Skipping {svc.name} (backend: {coding_backend}).")
            continue
        try:
            binaries = _resolve_binaries(config, [svc])
        except click.ClickException as exc:
            click.echo(click.style(f"  WARN  Skipping {svc.name}: {exc}", fg="yellow"))
            continue
        click.echo(f"  Regenerating {svc.name}...")
        _write_service_files(svc, config, binaries)

    click.echo("Regenerated all wrappers and plists.")
    click.echo("Run 'openbase-coder services install' to reload them.")
