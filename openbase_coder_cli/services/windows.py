"""Windows Task Scheduler backend (``schtasks.exe``), used on win32.

Mirrors ``systemd.py``'s role and shape exactly: a 5-function contract
(``windows_bootstrap/bootout/kickstart/kill/status``) that ``launchd.py``
dispatches to on Windows, the same way it already dispatches to
``systemd.py`` on Linux. No admin privileges required — tasks are
registered per-user with an ``ONLOGON`` trigger (``ONSTART`` needs
elevation).
"""

from __future__ import annotations

import subprocess
import textwrap
import time
from pathlib import Path

import click
import psutil

from openbase_coder_cli.paths import (
    OPENBASE_BASE_DIR,
    TASK_SCHEDULER_DIR,
    TASK_SCHEDULER_FOLDER,
)
from openbase_coder_cli.services.definitions import ServiceDefinition
from openbase_coder_cli.services.installation import InstallationConfig
from openbase_coder_cli.services.launchd import (
    _cleanup_lingering_processes,
    _external_supervisor,
    _external_supervisor_status,
    _prepare_service_start,
    _runtime_workdir,
    _service_label,
)

# Bounded retry/backoff, mirroring launchctl_bootstrap/systemd_bootstrap.
_BOOTSTRAP_ATTEMPTS = 4


def _task_name(svc: ServiceDefinition) -> str:
    return f"{TASK_SCHEDULER_FOLDER}\\{_service_label(svc)}"


def task_xml_path(svc: ServiceDefinition) -> Path:
    return TASK_SCHEDULER_DIR / f"{_service_label(svc)}.xml"


def _schtasks(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, check=check
    )


def generate_task_xml(
    svc: ServiceDefinition, config: InstallationConfig, python_bin: str
) -> Path:
    workdir = svc.workdir_template.format(
        workspace=config.workspace_path or _runtime_workdir(config),
        data_dir=str(OPENBASE_BASE_DIR),
        runtime_workdir=_runtime_workdir(config),
    )
    arguments = f"-m openbase_coder_cli.services.runners {svc.command_template}"

    xml_path = task_xml_path(svc)
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(
        textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-16"?>
        <Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <Triggers>
            <LogonTrigger>
              <Enabled>true</Enabled>
            </LogonTrigger>
          </Triggers>
          <Principals>
            <Principal id="Author">
              <LogonType>InteractiveToken</LogonType>
              <RunLevel>LeastPrivilege</RunLevel>
            </Principal>
          </Principals>
          <Settings>
            <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
            <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
            <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
            <StartWhenAvailable>true</StartWhenAvailable>
            <RestartOnFailure>
              <Interval>PT1M</Interval>
              <Count>3</Count>
            </RestartOnFailure>
          </Settings>
          <Actions Context="Author">
            <Exec>
              <Command>{python_bin}</Command>
              <Arguments>{arguments}</Arguments>
              <WorkingDirectory>{workdir}</WorkingDirectory>
            </Exec>
          </Actions>
        </Task>
        """),
        encoding="utf-16",
    )
    return xml_path


def windows_bootstrap(svc: ServiceDefinition) -> None:
    _prepare_service_start(svc)
    task = _task_name(svc)
    result: subprocess.CompletedProcess | None = None
    for attempt in range(_BOOTSTRAP_ATTEMPTS):
        result = _schtasks(
            "/Create", "/TN", task, "/XML", str(task_xml_path(svc)), "/F"
        )
        if result.returncode == 0:
            _schtasks("/Run", "/TN", task, check=False)
            return
        time.sleep(0.5 * (attempt + 1))
    detail = (result.stderr.strip() or result.stdout.strip()) if result else ""
    raise click.ClickException(f"Failed to start {task}: {detail}")


def windows_bootout(svc: ServiceDefinition) -> bool:
    task = _task_name(svc)
    existed = _schtasks("/Query", "/TN", task, check=False).returncode == 0
    _schtasks("/End", "/TN", task, check=False)
    _schtasks("/Delete", "/TN", task, "/F", check=False)
    _cleanup_lingering_processes(svc)
    return existed


def windows_kickstart(svc: ServiceDefinition) -> bool:
    _prepare_service_start(svc)
    result = _schtasks("/Run", "/TN", _task_name(svc), check=False)
    return result.returncode == 0


def windows_kill(svc: ServiceDefinition) -> bool:
    result = _schtasks("/End", "/TN", _task_name(svc), check=False)
    _cleanup_lingering_processes(svc)
    return result.returncode == 0


def _parse_schtasks_list(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        values[key.strip()] = value.strip()
    return values


def _find_pid(svc: ServiceDefinition) -> str | None:
    marker = f"openbase_coder_cli.services.runners {svc.command_template}"
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if marker in cmdline:
            return str(proc.info["pid"])
    return None


def windows_status(svc: ServiceDefinition) -> dict:
    if _external_supervisor():
        return _external_supervisor_status(svc)

    result = _schtasks(
        "/Query", "/TN", _task_name(svc), "/FO", "LIST", "/V", check=False
    )
    if result.returncode != 0:
        return {"installed": False}

    info = _parse_schtasks_list(result.stdout)
    status_text = info.get("Status", "")
    pid = _find_pid(svc) if status_text.strip().lower() == "running" else None
    return {
        "installed": True,
        "pid": pid,
        "last_exit_code": info.get("Last Result"),
    }
