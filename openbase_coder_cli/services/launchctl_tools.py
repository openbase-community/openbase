from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import click

from openbase_coder_cli.paths import LAUNCHD_DOMAIN
from openbase_coder_cli.services import process_utils
from openbase_coder_cli.services.console_settings import get_ignored_launchctl_labels
from openbase_coder_cli.services.voice_warning import warn_before_voice_interruption

LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
OPENBASE_LAUNCHCTL_PREFIX = "com.openbase.coder."
RESTART_EXIT_TIMEOUT_SECONDS = 15.0
VOICE_INTERRUPTING_LAUNCHCTL_LABELS = {
    f"{LAUNCHD_DOMAIN}.livekit-agent",
    f"{LAUNCHD_DOMAIN}.livekit-server",
}


@dataclass
class LaunchctlRuntimeJob:
    label: str
    pid: int | None
    status: int | None


@dataclass
class LaunchctlService:
    label: str
    plist_path: str
    loaded: bool
    running: bool
    pid: int | None
    status: int | None
    command: str | None
    program: str | None
    program_arguments: list[str]
    working_directory: str | None
    run_at_load: bool
    keep_alive: bool
    disabled: bool | None
    is_openbase_managed: bool
    plist_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "plist_path": self.plist_path,
            "loaded": self.loaded,
            "running": self.running,
            "pid": self.pid,
            "status": self.status,
            "command": self.command,
            "program": self.program,
            "program_arguments": self.program_arguments,
            "working_directory": self.working_directory,
            "run_at_load": self.run_at_load,
            "keep_alive": self.keep_alive,
            "disabled": self.disabled,
            "is_openbase_managed": self.is_openbase_managed,
            "plist_error": self.plist_error,
        }


def list_launchctl_services_payload(include_ignored: bool = False) -> dict:
    if platform.system() != "Darwin":
        from openbase_coder_cli.services.systemd import list_systemd_services_payload

        return list_systemd_services_payload(include_ignored)

    runtime_jobs, runtime_error = _list_runtime_jobs()
    services = _read_launch_agents(runtime_jobs)
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
        "error": runtime_error,
        "ignored_labels": sorted(ignored_labels),
    }


def run_launchctl_service_action(label: str, action: str) -> None:
    if platform.system() != "Darwin":
        from openbase_coder_cli.services.systemd import run_systemd_service_action

        run_systemd_service_action(label, action)
        return

    if action not in {"start", "stop", "restart"}:
        raise click.ClickException(f"Unsupported launchctl action '{action}'.")

    service = _find_launch_agent(label)
    domain = _launchctl_domain()
    if action in {"stop", "restart"} and label in VOICE_INTERRUPTING_LAUNCHCTL_LABELS:
        warn_before_voice_interruption(
            reason=f"launchctl {action} {label}",
            emit_cli_warning=False,
        )

    runtime_jobs, _ = _list_runtime_jobs()
    runtime_job = runtime_jobs.get(label)
    is_loaded = runtime_job is not None

    if action == "stop":
        if not is_loaded:
            return
        result = _run_launchctl("bootout", f"{domain}/{label}", check=False)
        _raise_for_launchctl_failure(result, f"Unable to stop {label}")
        return

    if action == "start":
        if is_loaded:
            result = _run_launchctl(
                "kickstart",
                "-k",
                f"{domain}/{label}",
                check=False,
            )
        else:
            result = _run_launchctl(
                "bootstrap",
                domain,
                service.plist_path,
                check=False,
            )
        _raise_for_launchctl_failure(result, f"Unable to start {label}")
        return

    if not is_loaded:
        result = _run_launchctl("bootstrap", domain, service.plist_path, check=False)
        _raise_for_launchctl_failure(result, f"Unable to restart {label}")
        return

    # Restart in place rather than bootout + bootstrap: re-registering the
    # job makes macOS post a "Background Items Added" notification each time.
    target = f"{domain}/{label}"
    old_pid = runtime_job.pid
    if old_pid is not None:
        _run_launchctl("kill", "SIGTERM", target, check=False)
        pid = process_utils.wait_for_pid_change(
            lambda: (
                _list_runtime_jobs()[0]
                .get(label, LaunchctlRuntimeJob(label, None, None))
                .pid
            ),
            old_pid,
            timeout=RESTART_EXIT_TIMEOUT_SECONDS,
        )
        if pid == old_pid:
            result = _run_launchctl("kickstart", "-k", target, check=False)
            _raise_for_launchctl_failure(result, f"Unable to restart {label}")
            return
    # No-op if KeepAlive already respawned the job; starts it otherwise.
    result = _run_launchctl("kickstart", target, check=False)
    _raise_for_launchctl_failure(result, f"Unable to restart {label}")


def _find_launch_agent(label: str) -> LaunchctlService:
    for service in _read_launch_agents({}):
        if service.label == label:
            return service
    raise click.ClickException(
        f"LaunchAgent '{label}' was not found in {LAUNCH_AGENTS_DIR}."
    )


def _read_launch_agents(
    runtime_jobs: dict[str, LaunchctlRuntimeJob],
) -> list[LaunchctlService]:
    services: list[LaunchctlService] = []

    if not LAUNCH_AGENTS_DIR.is_dir():
        return services

    for plist_path in sorted(LAUNCH_AGENTS_DIR.glob("*.plist")):
        services.append(_launch_agent_from_plist(plist_path, runtime_jobs))

    return services


def _launch_agent_from_plist(
    plist_path: Path,
    runtime_jobs: dict[str, LaunchctlRuntimeJob],
) -> LaunchctlService:
    try:
        with plist_path.open("rb") as handle:
            plist_data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        runtime_job = runtime_jobs.get(plist_path.stem)
        return LaunchctlService(
            label=plist_path.stem,
            plist_path=str(plist_path),
            loaded=runtime_job is not None,
            running=runtime_job is not None and runtime_job.pid is not None,
            pid=runtime_job.pid if runtime_job else None,
            status=runtime_job.status if runtime_job else None,
            command=None,
            program=None,
            program_arguments=[],
            working_directory=None,
            run_at_load=False,
            keep_alive=False,
            disabled=None,
            is_openbase_managed=plist_path.stem.startswith(OPENBASE_LAUNCHCTL_PREFIX),
            plist_error=str(exc),
        )

    label = str(plist_data.get("Label") or plist_path.stem)
    runtime_job = runtime_jobs.get(label)
    program = _optional_string(plist_data.get("Program"))
    program_arguments = _string_list(plist_data.get("ProgramArguments"))
    command = _command_preview(program, program_arguments)

    return LaunchctlService(
        label=label,
        plist_path=str(plist_path),
        loaded=runtime_job is not None,
        running=runtime_job is not None and runtime_job.pid is not None,
        pid=runtime_job.pid if runtime_job else None,
        status=runtime_job.status if runtime_job else None,
        command=command,
        program=program,
        program_arguments=program_arguments,
        working_directory=_optional_string(plist_data.get("WorkingDirectory")),
        run_at_load=bool(plist_data.get("RunAtLoad")),
        keep_alive=_plist_truthy(plist_data.get("KeepAlive")),
        disabled=_optional_bool(plist_data.get("Disabled")),
        is_openbase_managed=label.startswith(OPENBASE_LAUNCHCTL_PREFIX),
    )


def _list_runtime_jobs() -> tuple[dict[str, LaunchctlRuntimeJob], str | None]:
    result = _run_launchctl("list", check=False)
    if result.returncode != 0:
        return {}, _launchctl_error_detail(result)

    jobs: dict[str, LaunchctlRuntimeJob] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("PID"):
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            parts = line.split(None, 2)
        if len(parts) != 3:
            continue

        pid_text, status_text, label = parts
        label = label.strip()
        if not label:
            continue
        jobs[label] = LaunchctlRuntimeJob(
            label=label,
            pid=_parse_optional_int(pid_text),
            status=_parse_optional_int(status_text),
        )

    return jobs, None


def _parse_optional_int(raw_value: str) -> int | None:
    value = raw_value.strip()
    if not value or value == "-":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _command_preview(program: str | None, program_arguments: list[str]) -> str | None:
    if program_arguments:
        return " ".join(program_arguments)
    return program


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item:
            result.append(item)
    return result


def _plist_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return True
    return False


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _run_launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    launchctl_bin = shutil.which("launchctl") or "/bin/launchctl"
    return subprocess.run(
        [launchctl_bin, *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _raise_for_launchctl_failure(
    result: subprocess.CompletedProcess,
    message: str,
) -> None:
    if result.returncode == 0:
        return
    raise click.ClickException(f"{message}: {_launchctl_error_detail(result)}")


def _launchctl_error_detail(result: subprocess.CompletedProcess) -> str:
    return result.stderr.strip() or result.stdout.strip() or "launchctl command failed."
