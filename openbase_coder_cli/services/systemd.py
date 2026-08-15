"""systemd user-unit backend used on Linux (launchd is used on macOS)."""

from __future__ import annotations

import subprocess
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import click

from openbase_coder_cli.paths import (
    LAUNCHD_DOMAIN,
    OPENBASE_BASE_DIR,
    SYSTEMD_UNIT_DIR,
)
from openbase_coder_cli.services.console_settings import get_ignored_launchctl_labels
from openbase_coder_cli.services.definitions import ServiceDefinition
from openbase_coder_cli.services.installation import InstallationConfig
from openbase_coder_cli.services.voice_warning import warn_before_voice_interruption

OPENBASE_UNIT_PREFIX = f"{LAUNCHD_DOMAIN}."
VOICE_INTERRUPTING_SERVICE_LABELS = {
    f"{LAUNCHD_DOMAIN}.livekit-agent",
    f"{LAUNCHD_DOMAIN}.livekit-server",
}


def _service_label(svc: ServiceDefinition) -> str:
    return f"{LAUNCHD_DOMAIN}.{svc.name}"


def _unit_name(svc: ServiceDefinition) -> str:
    return f"{_service_label(svc)}.service"


def unit_path(svc: ServiceDefinition) -> Path:
    return SYSTEMD_UNIT_DIR / _unit_name(svc)


def _systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    argv = ["systemctl", "--user", *args]
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=check,
        )
    except FileNotFoundError as exc:
        # Containers and minimal Linux hosts have no systemd; degrade to the
        # same shape as a failed systemctl call so status checks report
        # "not installed" instead of crashing.
        if check:
            raise subprocess.CalledProcessError(
                127, argv, output="", stderr="systemctl not found"
            ) from exc
        return subprocess.CompletedProcess(
            argv, returncode=127, stdout="", stderr="systemctl not found"
        )


def _show_properties(unit: str, properties: tuple[str, ...]) -> dict[str, str]:
    result = _systemctl("show", unit, "-p", ",".join(properties))
    values: dict[str, str] = {}
    if result.returncode != 0:
        return values
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            values[key.strip()] = value.strip()
    return values


def generate_unit(svc: ServiceDefinition, config: InstallationConfig) -> Path:
    from openbase_coder_cli.services.launchd import (
        _log_path,
        _runtime_workdir,
        _wrapper_path,
    )

    wrapper = _wrapper_path(svc)
    workdir = svc.workdir_template.format(
        workspace=config.workspace_path or _runtime_workdir(config),
        data_dir=str(OPENBASE_BASE_DIR),
        runtime_workdir=_runtime_workdir(config),
    )
    log_path = _log_path(svc)

    unit = unit_path(svc)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        textwrap.dedent(f"""\
        # Auto-generated unit for {svc.name}
        [Unit]
        Description={svc.description}

        [Service]
        Type={svc.service_type}
        ExecStart=/bin/bash {wrapper}
        WorkingDirectory={workdir}
        {_restart_lines(svc)}
        StandardOutput=append:{log_path}
        StandardError=append:{log_path}

        [Install]
        WantedBy=default.target
    """)
    )
    return unit


def _restart_lines(svc: ServiceDefinition) -> str:
    if not svc.restart_policy:
        return ""
    return f"Restart={svc.restart_policy}\n        RestartSec=5"


def remove_unit(svc: ServiceDefinition) -> None:
    unit_path(svc).unlink(missing_ok=True)
    _systemctl("daemon-reload")


def systemd_bootstrap(svc: ServiceDefinition) -> None:
    from openbase_coder_cli.services.launchd import _prepare_service_start

    unit = _unit_name(svc)
    _prepare_service_start(svc)
    _systemctl("daemon-reload")
    _systemctl("enable", unit)
    result: subprocess.CompletedProcess | None = None
    for attempt in range(4):
        _systemctl("reset-failed", unit)
        result = _systemctl("restart", unit)
        if result.returncode == 0:
            return
        time.sleep(0.5 * (attempt + 1))
    detail = (result.stderr.strip() or result.stdout.strip()) if result else ""
    raise click.ClickException(f"Failed to start {unit}: {detail}")


def _unit_loaded(unit: str) -> bool:
    return _show_properties(unit, ("LoadState",)).get("LoadState") == "loaded"


def systemd_bootout(svc: ServiceDefinition) -> bool:
    from openbase_coder_cli.services.launchd import _cleanup_lingering_processes

    unit = _unit_name(svc)
    loaded = _unit_loaded(unit)
    _systemctl("disable", "--now", unit)
    _cleanup_lingering_processes(svc)
    return loaded


def systemd_kickstart(svc: ServiceDefinition) -> bool:
    from openbase_coder_cli.services.launchd import _prepare_service_start

    _prepare_service_start(svc)
    _systemctl("reset-failed", _unit_name(svc))
    result = _systemctl("restart", _unit_name(svc))
    return result.returncode == 0


def systemd_kill(svc: ServiceDefinition) -> bool:
    from openbase_coder_cli.services.launchd import _cleanup_lingering_processes

    result = _systemctl("kill", "--signal=SIGTERM", _unit_name(svc))
    _cleanup_lingering_processes(svc)
    return result.returncode == 0


def systemd_status(svc: ServiceDefinition) -> dict:
    unit = _unit_name(svc)
    props = _show_properties(
        unit, ("LoadState", "ActiveState", "MainPID", "ExecMainStatus")
    )
    if props.get("LoadState") != "loaded":
        return {"installed": False}

    pid = props.get("MainPID", "0")
    last_exit = props.get("ExecMainStatus")
    return {
        "installed": True,
        "pid": pid if pid and pid != "0" else None,
        "last_exit_code": last_exit,
    }


@dataclass
class SystemdUserService:
    label: str
    unit_path: str
    loaded: bool
    running: bool
    pid: int | None
    status: int | None
    command: str | None
    working_directory: str | None
    keep_alive: bool
    is_openbase_managed: bool

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "plist_path": self.unit_path,
            "loaded": self.loaded,
            "running": self.running,
            "pid": self.pid,
            "status": self.status,
            "command": self.command,
            "program": None,
            "program_arguments": [],
            "working_directory": self.working_directory,
            "run_at_load": True,
            "keep_alive": self.keep_alive,
            "disabled": None,
            "is_openbase_managed": self.is_openbase_managed,
            "plist_error": None,
        }


def list_systemd_services_payload(include_ignored: bool = False) -> dict:
    services = [_read_user_unit(path) for path in _user_unit_paths()]
    ignored_labels = set(get_ignored_launchctl_labels())
    if not include_ignored:
        services = [
            service for service in services if service.label not in ignored_labels
        ]
    services.sort(
        key=lambda service: (not service.running, not service.loaded, service.label)
    )
    return {
        "services": [service.to_dict() for service in services],
        "error": None,
        "ignored_labels": sorted(ignored_labels),
    }


def run_systemd_service_action(label: str, action: str) -> None:
    if action not in {"start", "stop", "restart"}:
        raise click.ClickException(f"Unsupported service action '{action}'.")

    unit = f"{label}.service"
    if not (SYSTEMD_UNIT_DIR / unit).is_file():
        raise click.ClickException(
            f"systemd user unit '{label}' was not found in {SYSTEMD_UNIT_DIR}."
        )

    if action in {"stop", "restart"} and label in VOICE_INTERRUPTING_SERVICE_LABELS:
        warn_before_voice_interruption(
            reason=f"systemctl {action} {label}",
            emit_cli_warning=False,
        )

    _systemctl("daemon-reload")
    if action == "stop":
        result = _systemctl("stop", unit)
    else:
        _systemctl("reset-failed", unit)
        result = _systemctl("restart", unit)
    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or "systemctl command failed."
        )
        raise click.ClickException(f"Unable to {action} {label}: {detail}")


def _user_unit_paths() -> list[Path]:
    if not SYSTEMD_UNIT_DIR.is_dir():
        return []
    return sorted(path for path in SYSTEMD_UNIT_DIR.glob("*.service") if path.is_file())


def _read_user_unit(path: Path) -> SystemdUserService:
    label = path.name.removesuffix(".service")
    unit = path.name
    props = _show_properties(
        unit,
        ("LoadState", "ActiveState", "MainPID", "ExecMainStatus", "Restart"),
    )
    pid_text = props.get("MainPID", "0")
    pid = int(pid_text) if pid_text.isdigit() and pid_text != "0" else None
    status_text = props.get("ExecMainStatus", "")
    command, working_directory = _parse_unit_file(path)

    return SystemdUserService(
        label=label,
        unit_path=str(path),
        loaded=props.get("LoadState") == "loaded",
        running=props.get("ActiveState") == "active",
        pid=pid,
        status=int(status_text) if status_text.lstrip("-").isdigit() else None,
        command=command,
        working_directory=working_directory,
        keep_alive=props.get("Restart", "no") not in ("", "no"),
        is_openbase_managed=label.startswith(OPENBASE_UNIT_PREFIX),
    )


def _parse_unit_file(path: Path) -> tuple[str | None, str | None]:
    command: str | None = None
    working_directory: str | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("ExecStart=") and command is None:
            command = stripped.partition("=")[2].strip() or None
        elif stripped.startswith("WorkingDirectory=") and working_directory is None:
            working_directory = stripped.partition("=")[2].strip() or None
    return command, working_directory
