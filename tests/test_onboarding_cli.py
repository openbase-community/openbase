from __future__ import annotations

import importlib
import json
import os
from types import SimpleNamespace

from click.testing import CliRunner

from openbase_coder_cli.cli import main
from openbase_coder_cli.services.cloud_registration import CloudReportResult

onboarding_cli = importlib.import_module("openbase_coder_cli.cli.onboarding")
setup_cli = importlib.import_module("openbase_coder_cli.cli.setup")


def _fake_status_payload() -> dict:
    return {
        "cli_configured": True,
        "checks": {
            "installation_config": True,
            "env_file": True,
            "services_installed": False,
        },
        "authenticated": True,
        "backend_auth": {"backend": "codex", "ready": False},
        "tailscale_self": {
            "available": True,
            "tailscale_available": True,
            "dns_name": "mac.tailnet.ts.net",
            "node_hostname": "mac",
            "tailnet": "tailnet.ts.net",
            "ips": ["100.64.0.1"],
            "error": None,
        },
        "tailscale_serve": {
            "healthy": False,
            "openbase_url": "http://mac.tailnet.ts.net:18080",
            "error": "not reachable",
        },
        "cloud": {},
    }


def test_onboarding_status_json(monkeypatch) -> None:
    monkeypatch.setattr(
        onboarding_cli, "onboarding_status_payload", _fake_status_payload
    )

    result = CliRunner().invoke(main, ["onboarding", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == _fake_status_payload()


def test_onboarding_status_human_readable(monkeypatch) -> None:
    monkeypatch.setattr(
        onboarding_cli, "onboarding_status_payload", _fake_status_payload
    )

    result = CliRunner().invoke(main, ["onboarding", "status"])

    assert result.exit_code == 0
    assert "cli_configured" in result.output
    assert "services_installed" in result.output
    assert "backend auth (codex)" in result.output
    assert "mac.tailnet.ts.net" in result.output
    assert "not reachable" in result.output


def test_onboarding_report_success(monkeypatch) -> None:
    monkeypatch.setattr(
        onboarding_cli,
        "register_and_report",
        lambda: CloudReportResult(ok=True, supported=True, status_code=200),
    )

    result = CliRunner().invoke(main, ["onboarding", "report"])

    assert result.exit_code == 0
    assert "Registered device" in result.output


def test_onboarding_report_skips_when_unsupported(monkeypatch) -> None:
    monkeypatch.setattr(
        onboarding_cli,
        "register_and_report",
        lambda: CloudReportResult(ok=False, supported=False, error="not supported yet"),
    )

    result = CliRunner().invoke(main, ["onboarding", "report"])

    assert result.exit_code == 0
    assert "Skipped" in result.output


def test_onboarding_report_fails_on_supported_error(monkeypatch) -> None:
    monkeypatch.setattr(
        onboarding_cli,
        "register_and_report",
        lambda: CloudReportResult(ok=False, supported=True, error="HTTP 500"),
    )

    result = CliRunner().invoke(main, ["onboarding", "report"])

    assert result.exit_code != 0
    assert "HTTP 500" in result.output


def test_setup_has_json_progress_option() -> None:
    result = CliRunner().invoke(main, ["setup", "--help"])

    assert result.exit_code == 0
    assert "--json-progress" in result.output


def test_setup_progress_emits_ndjson_events() -> None:
    read_fd, write_fd = os.pipe()
    progress = setup_cli._SetupProgress(False)
    progress.enabled = True
    progress._fd = write_fd

    progress.step("workspace", "start")
    progress.step("workspace", "ok")
    progress.step("tailscale_serve", "warn", "tailscale missing")
    progress.result(cli_configured=True, tailscale_serve_healthy=False)
    os.close(write_fd)

    with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
        events = [json.loads(line) for line in reader.read().splitlines()]

    assert events[0] == {
        "event": "step",
        "id": "workspace",
        "status": "start",
        "detail": None,
    }
    assert events[1]["status"] == "ok"
    assert events[2] == {
        "event": "step",
        "id": "tailscale_serve",
        "status": "warn",
        "detail": "tailscale missing",
    }
    assert events[3] == {
        "event": "result",
        "ok": True,
        "cli_configured": True,
        "tailscale_serve_healthy": False,
    }


def test_setup_progress_abort_reports_current_step() -> None:
    read_fd, write_fd = os.pipe()
    progress = setup_cli._SetupProgress(False)
    progress.enabled = True
    progress._fd = write_fd

    progress.step("services", "start")
    progress.abort("boom")
    os.close(write_fd)

    with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
        events = [json.loads(line) for line in reader.read().splitlines()]

    assert events[1] == {
        "event": "step",
        "id": "services",
        "status": "error",
        "detail": "boom",
    }
    assert events[2]["event"] == "result"
    assert events[2]["ok"] is False


def test_setup_progress_disabled_is_noop() -> None:
    progress = setup_cli._SetupProgress(False)
    progress.step("workspace", "start")
    progress.result(cli_configured=True, tailscale_serve_healthy=True)
    progress.abort("boom")


def test_setup_result_uses_computed_cli_state(monkeypatch, capfd) -> None:
    monkeypatch.setattr("openbase_coder_cli.platforms.current_system", lambda: "Darwin")
    monkeypatch.setattr(setup_cli, "_run_setup_phases", lambda *args, **kwargs: True)
    monkeypatch.setattr(setup_cli, "compute_cli_configured", lambda: False)

    result = CliRunner().invoke(
        main,
        ["setup", "--json-progress", "--backend", "codex", "--skip-services"],
    )

    assert result.exit_code == 0
    events = [
        json.loads(line) for line in capfd.readouterr().out.splitlines() if line.strip()
    ]
    assert events[-1] == {
        "event": "result",
        "ok": True,
        "cli_configured": False,
        "tailscale_serve_healthy": True,
    }


_SETUP_PHASE_NOOP_NAMES = (
    "_ensure_thread_sync_exchange_dir",
    "_ensure_bundled_sounds",
    "_ensure_env_file",
    "ensure_backend_binary",
    "ensure_pinned_livekit_server",
    "_ensure_claude_md_symlink",
    "_ensure_openbase_instruction_files",
    "_ensure_codex_home_dispatcher_config",
    "set_dispatcher_service_tier",
    "_symlink_codex_home_skills",
    "_init_cli_workspace",
    "_ensure_session_id_hook_script",
    "_ensure_codex_config",
    "_ensure_claude_mcp",
    "_ensure_claude_hooks",
    "_install_cli_shim",
    "_build_console",
    "install_all_services",
    "configure_tailscale_serve",
)


def _patch_setup_phase_collaborators(monkeypatch, tmp_path, workspace) -> None:
    class FakeInstallationConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def save(self) -> None:
            return None

    def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(setup_cli, "OPENBASE_BASE_DIR", tmp_path / ".openbase")
    monkeypatch.setattr(setup_cli, "InstallationConfig", FakeInstallationConfig)
    monkeypatch.setattr(setup_cli, "current_runtime_package", lambda: None)
    monkeypatch.setattr(
        setup_cli, "resolve_dev_workspace_dir", lambda value: str(workspace)
    )
    for name in _SETUP_PHASE_NOOP_NAMES:
        monkeypatch.setattr(setup_cli, name, noop)


def test_setup_phases_do_not_report_to_cloud(monkeypatch, tmp_path) -> None:
    class CaptureProgress:
        def __init__(self):
            self.events = []

        def step(self, step_id: str, step_status: str, detail: str | None = None):
            self.events.append((step_id, step_status, detail))

    progress = CaptureProgress()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _patch_setup_phase_collaborators(monkeypatch, tmp_path, workspace)
    monkeypatch.setattr(setup_cli, "_selected_coding_backend", lambda *args: "codex")
    monkeypatch.setattr(
        setup_cli,
        "tailscale_serve_health",
        lambda: SimpleNamespace(
            healthy=True, openbase_url="http://mac.tailnet.ts.net", error=None
        ),
    )

    def fail_cloud_report(*args, **kwargs):
        raise AssertionError("setup must not report onboarding state to Openbase Cloud")

    monkeypatch.setattr(
        setup_cli, "register_and_report", fail_cloud_report, raising=False
    )

    serve_healthy = setup_cli._run_setup_phases(
        progress,
        workspace_dir=str(workspace),
        env_file=str(tmp_path / ".env"),
        assembly_ai_api_key="",
        cartesia_api_key="",
        skip_services=True,
        fast_mode=True,
        coding_backend="codex",
        audio_provider="openbase-cloud",
    )

    assert serve_healthy is True
    assert all(event[0] != "cloud_report" for event in progress.events)


def test_openbase_cloud_setup_provisions_machine_token(monkeypatch, tmp_path) -> None:
    class CaptureProgress:
        enabled = False

        def step(self, step_id: str, step_status: str, detail: str | None = None):
            return None

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / ".env"
    machine_token_calls: list[bool] = []
    codex_config_calls: list[dict] = []

    _patch_setup_phase_collaborators(monkeypatch, tmp_path, workspace)
    monkeypatch.setattr(
        setup_cli,
        "_ensure_openbase_cloud_machine_token",
        lambda _env_file: machine_token_calls.append(True),
    )
    monkeypatch.setattr(
        setup_cli,
        "_selected_coding_backend",
        lambda *args: "openbase_cloud",
    )

    def capture_codex_config(*args, **kwargs):
        codex_config_calls.append(kwargs)

    monkeypatch.setattr(setup_cli, "_ensure_codex_config", capture_codex_config)
    monkeypatch.setattr(
        setup_cli,
        "tailscale_serve_health",
        lambda: SimpleNamespace(
            healthy=True, openbase_url="http://workspace", error=None
        ),
    )

    setup_cli._run_setup_phases(
        CaptureProgress(),
        workspace_dir=str(workspace),
        env_file=str(env_file),
        assembly_ai_api_key="",
        cartesia_api_key="",
        skip_services=False,
        fast_mode=True,
        coding_backend="openbase_cloud",
        audio_provider="openbase-cloud",
    )

    assert machine_token_calls == [True]
    assert codex_config_calls[-1]["coding_backend"] == "openbase_cloud"


def test_setup_never_runs_interactive_claude_login(
    monkeypatch, tmp_path, capfd
) -> None:
    """Setup only warns about a missing Claude login; it never launches one."""

    class CaptureProgress:
        enabled = True

        def step(self, step_id: str, step_status: str, detail: str | None = None):
            return None

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _patch_setup_phase_collaborators(monkeypatch, tmp_path, workspace)
    monkeypatch.setattr(
        setup_cli, "_selected_coding_backend", lambda *args: "claude_code"
    )
    monkeypatch.setattr(
        setup_cli,
        "claude_auth_status",
        lambda: SimpleNamespace(logged_in=False, raw_output="", returncode=1),
    )

    def fail_interactive_login(*args, **kwargs):
        raise AssertionError("setup must not run an interactive claude login")

    monkeypatch.setattr(setup_cli, "run_claude_login", fail_interactive_login)
    monkeypatch.setattr(
        setup_cli,
        "tailscale_serve_health",
        lambda: SimpleNamespace(healthy=True, openbase_url="", error=None),
    )

    setup_cli._run_setup_phases(
        CaptureProgress(),
        workspace_dir=str(workspace),
        env_file=str(tmp_path / ".env"),
        assembly_ai_api_key="",
        cartesia_api_key="",
        skip_services=True,
        fast_mode=True,
        coding_backend="claude_code",
        audio_provider="openbase-cloud",
    )

    assert "Claude Code is not logged in" in capfd.readouterr().out
