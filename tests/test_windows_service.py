from __future__ import annotations

import subprocess

from openbase_coder_cli.services import windows
from openbase_coder_cli.services.definitions import ServiceDefinition
from openbase_coder_cli.services.installation import InstallationConfig


def _sample_service() -> ServiceDefinition:
    return ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="sample",
        workdir_template="{workspace}",
    )


def test_generate_task_xml_writes_logon_trigger_task(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, "TASK_SCHEDULER_DIR", tmp_path / "tasks")
    monkeypatch.setattr(windows, "OPENBASE_BASE_DIR", tmp_path / "openbase")
    config = InstallationConfig(
        workspace_path=str(tmp_path / "workspace"), env_file=str(tmp_path / ".env")
    )

    xml_path = windows.generate_task_xml(
        _sample_service(), config, r"C:\python\python.exe"
    )

    content = xml_path.read_text(encoding="utf-16")
    assert "<LogonTrigger>" in content
    assert r"C:\python\python.exe" in content.replace("&#92;", "\\")
    assert "openbase_coder_cli.services.runners sample" in content
    assert "<RunLevel>LeastPrivilege</RunLevel>" in content


def test_windows_bootstrap_creates_and_runs_task(monkeypatch):
    service = _sample_service()
    calls: list[tuple[str, ...]] = []

    def fake_schtasks(*args, check=False):
        calls.append(args)
        return subprocess.CompletedProcess(["schtasks", *args], 0, "", "")

    monkeypatch.setattr(windows, "_schtasks", fake_schtasks)
    monkeypatch.setattr(windows, "_prepare_service_start", lambda _svc: None)

    windows.windows_bootstrap(service)

    create_calls = [c for c in calls if c[0] == "/Create"]
    run_calls = [c for c in calls if c[0] == "/Run"]
    assert len(create_calls) == 1
    assert len(run_calls) == 1
    assert windows._task_name(service) in create_calls[0]


def test_windows_status_reports_not_installed(monkeypatch):
    monkeypatch.setattr(windows, "_external_supervisor", lambda: False)
    monkeypatch.setattr(
        windows,
        "_schtasks",
        lambda *a, **k: subprocess.CompletedProcess(["schtasks", *a], 1, "", "ERROR"),
    )

    assert windows.windows_status(_sample_service()) == {"installed": False}


def test_windows_status_reports_running_with_pid(monkeypatch):
    monkeypatch.setattr(windows, "_external_supervisor", lambda: False)
    monkeypatch.setattr(
        windows,
        "_schtasks",
        lambda *a, **k: subprocess.CompletedProcess(
            ["schtasks", *a],
            0,
            "Status:               Running\nLast Result:          0\n",
            "",
        ),
    )
    monkeypatch.setattr(windows, "_find_pid", lambda _svc: "4242")

    status = windows.windows_status(_sample_service())

    assert status == {"installed": True, "pid": "4242", "last_exit_code": "0"}


def test_windows_status_reports_stopped_without_pid(monkeypatch):
    monkeypatch.setattr(windows, "_external_supervisor", lambda: False)
    monkeypatch.setattr(
        windows,
        "_schtasks",
        lambda *a, **k: subprocess.CompletedProcess(
            ["schtasks", *a],
            0,
            "Status:               Ready\nLast Result:          0\n",
            "",
        ),
    )
    monkeypatch.setattr(windows, "_find_pid", lambda _svc: "9999")

    status = windows.windows_status(_sample_service())

    assert status == {"installed": True, "pid": None, "last_exit_code": "0"}


def test_windows_bootstrap_retries_on_transient_create_failure(monkeypatch):
    service = _sample_service()
    calls: list[tuple[str, ...]] = []
    returncodes = iter([1, 1, 0])

    def fake_schtasks(*args, check=False):
        calls.append(args)
        code = next(returncodes) if args[0] == "/Create" else 0
        return subprocess.CompletedProcess(["schtasks", *args], code, "", "busy")

    monkeypatch.setattr(windows, "_schtasks", fake_schtasks)
    monkeypatch.setattr(windows, "_prepare_service_start", lambda _svc: None)
    monkeypatch.setattr(windows.time, "sleep", lambda _seconds: None)

    windows.windows_bootstrap(service)

    create_calls = [c for c in calls if c[0] == "/Create"]
    run_calls = [c for c in calls if c[0] == "/Run"]
    assert len(create_calls) == 3
    assert len(run_calls) == 1


def test_windows_bootstrap_raises_after_exhausting_attempts(monkeypatch):
    service = _sample_service()

    def always_fails(*args, check=False):
        return subprocess.CompletedProcess(
            ["schtasks", *args], 1, "", "access denied"
        )

    monkeypatch.setattr(windows, "_schtasks", always_fails)
    monkeypatch.setattr(windows, "_prepare_service_start", lambda _svc: None)
    monkeypatch.setattr(windows.time, "sleep", lambda _seconds: None)

    try:
        windows.windows_bootstrap(service)
        raised = False
    except Exception as exc:  # click.ClickException
        raised = True
        assert "access denied" in str(exc)
    assert raised


def test_windows_bootout_terminates_process_and_cleans_up(monkeypatch):
    service = _sample_service()
    calls: list[tuple[str, ...]] = []
    cleanup_calls = []

    def fake_schtasks(*args, check=False):
        calls.append(args)
        return subprocess.CompletedProcess(["schtasks", *args], 0, "", "")

    monkeypatch.setattr(windows, "_schtasks", fake_schtasks)
    monkeypatch.setattr(
        windows, "_cleanup_lingering_processes", lambda svc: cleanup_calls.append(svc)
    )

    result = windows.windows_bootout(service)

    assert result is True
    assert any(c[0] == "/End" for c in calls)
    assert any(c[0] == "/Delete" for c in calls)
    assert cleanup_calls == [service]


def test_windows_kill_terminates_process_and_cleans_up(monkeypatch):
    service = _sample_service()
    cleanup_calls = []
    monkeypatch.setattr(
        windows,
        "_schtasks",
        lambda *a, **k: subprocess.CompletedProcess(["schtasks", *a], 0, "", ""),
    )
    monkeypatch.setattr(
        windows, "_cleanup_lingering_processes", lambda svc: cleanup_calls.append(svc)
    )

    assert windows.windows_kill(service) is True
    assert cleanup_calls == [service]


def test_windows_status_external_supervisor_defers_to_pid_file(monkeypatch):
    monkeypatch.setattr(windows, "_external_supervisor", lambda: True)
    monkeypatch.setattr(
        windows,
        "_external_supervisor_status",
        lambda svc: {"installed": True, "pid": "55"},
    )
    schtasks_called = []
    monkeypatch.setattr(
        windows, "_schtasks", lambda *a, **k: schtasks_called.append(a)
    )

    status = windows.windows_status(_sample_service())

    assert status == {"installed": True, "pid": "55"}
    # Task Scheduler is never consulted under external supervision.
    assert schtasks_called == []


def test_windows_status_defers_to_external_supervisor(monkeypatch):
    monkeypatch.setattr(windows, "_external_supervisor", lambda: True)
    monkeypatch.setattr(
        windows,
        "_external_supervisor_status",
        lambda _svc: {"installed": True, "pid": "77"},
    )

    assert windows.windows_status(_sample_service()) == {
        "installed": True,
        "pid": "77",
    }
