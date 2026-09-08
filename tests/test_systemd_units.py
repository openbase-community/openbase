import subprocess

from openbase_coder_cli.services import launchd, systemd
from openbase_coder_cli.services.definitions import ServiceDefinition
from openbase_coder_cli.services.installation import InstallationConfig


def _sample_service() -> ServiceDefinition:
    return ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="command -v openbase-coder",
        workdir_template="{workspace}",
    )


def test_generate_unit_writes_systemd_user_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "LAUNCHD_WRAPPER_DIR", tmp_path / "launchd")
    monkeypatch.setattr(launchd, "DEFAULT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(systemd, "SYSTEMD_UNIT_DIR", tmp_path / "systemd")
    monkeypatch.setattr(systemd, "OPENBASE_BASE_DIR", tmp_path / "openbase")

    config = InstallationConfig(
        workspace_path=str(tmp_path / "workspace"),
        env_file=str(tmp_path / ".env"),
    )

    unit = systemd.generate_unit(_sample_service(), config)

    assert unit.name == "com.openbase.coder.sample.service"
    text = unit.read_text()
    assert f"ExecStart=/bin/bash {tmp_path / 'launchd' / 'sample.sh'}" in text
    assert f"WorkingDirectory={tmp_path / 'workspace'}" in text
    assert "Restart=always" in text
    assert f"StandardOutput=append:{tmp_path / 'logs' / 'sample.log'}" in text
    assert "WantedBy=default.target" in text


def test_generate_unit_supports_boot_oneshot(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "LAUNCHD_WRAPPER_DIR", tmp_path / "launchd")
    monkeypatch.setattr(launchd, "DEFAULT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(systemd, "SYSTEMD_UNIT_DIR", tmp_path / "systemd")
    monkeypatch.setattr(systemd, "OPENBASE_BASE_DIR", tmp_path / "openbase")
    config = InstallationConfig(
        workspace_path=str(tmp_path / "workspace"),
        env_file=str(tmp_path / ".env"),
    )
    service = ServiceDefinition(
        name="boot-once",
        description="Boot once",
        command_template="true",
        workdir_template="{workspace}",
        service_type="oneshot",
        restart_policy="on-failure",
        keep_alive=False,
    )

    unit = systemd.generate_unit(service, config)

    text = unit.read_text()
    assert "Type=oneshot" in text
    assert "Restart=on-failure" in text


def test_systemd_status_parses_show_output(monkeypatch):
    def fake_systemctl(*args, check=False):
        return subprocess.CompletedProcess(
            ["systemctl", "--user", *args],
            0,
            "LoadState=loaded\nActiveState=active\nMainPID=123\nExecMainStatus=0\n",
            "",
        )

    monkeypatch.setattr(systemd, "_systemctl", fake_systemctl)

    status = systemd.systemd_status(_sample_service())

    assert status == {"installed": True, "pid": "123", "last_exit_code": "0"}


def test_systemd_status_not_installed(monkeypatch):
    def fake_systemctl(*args, check=False):
        return subprocess.CompletedProcess(
            ["systemctl", "--user", *args],
            0,
            "LoadState=not-found\nActiveState=inactive\nMainPID=0\nExecMainStatus=0\n",
            "",
        )

    monkeypatch.setattr(systemd, "_systemctl", fake_systemctl)

    assert systemd.systemd_status(_sample_service()) == {"installed": False}


def test_launchctl_status_dispatches_to_systemd_off_macos(monkeypatch):
    monkeypatch.setattr(launchd, "_is_macos", lambda: False)
    monkeypatch.setattr(launchd, "_is_windows", lambda: False)
    monkeypatch.setattr(
        systemd, "systemd_status", lambda svc: {"installed": True, "pid": "7"}
    )

    assert launchd.launchctl_status(_sample_service()) == {
        "installed": True,
        "pid": "7",
    }


def test_list_systemd_services_payload_reads_user_units(tmp_path, monkeypatch):
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    (unit_dir / "com.openbase.coder.sample.service").write_text(
        "[Unit]\nDescription=Sample\n\n[Service]\nExecStart=/bin/bash /tmp/sample.sh\n"
        "WorkingDirectory=/tmp\nRestart=always\n"
    )
    monkeypatch.setattr(systemd, "SYSTEMD_UNIT_DIR", unit_dir)
    monkeypatch.setattr(systemd, "get_ignored_launchctl_labels", lambda: [])

    def fake_systemctl(*args, check=False):
        return subprocess.CompletedProcess(
            ["systemctl", "--user", *args],
            0,
            "LoadState=loaded\nActiveState=active\nMainPID=42\nExecMainStatus=0\nRestart=always\n",
            "",
        )

    monkeypatch.setattr(systemd, "_systemctl", fake_systemctl)

    payload = systemd.list_systemd_services_payload()

    assert payload["error"] is None
    (service,) = payload["services"]
    assert service["label"] == "com.openbase.coder.sample"
    assert service["running"] is True
    assert service["pid"] == 42
    assert service["is_openbase_managed"] is True
    assert service["command"] == "/bin/bash /tmp/sample.sh"
