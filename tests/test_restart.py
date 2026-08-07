import importlib

from click.testing import CliRunner

from openbase_coder_cli.cli.restart import restart, self_restart
from openbase_coder_cli.services.definitions import default_services
from openbase_coder_cli.services.installation import InstallationConfig
from openbase_coder_cli.services.restart import (
    RestartPlan,
    RestartRequest,
    build_restart_plan,
    execute_restart_plan,
)

restart_module = importlib.import_module("openbase_coder_cli.services.restart")


def test_restart_default_schedules_all_openbase_services(monkeypatch):
    popen_calls = []
    warnings = []

    class FakePopen:
        def __init__(self, *args, **kwargs):
            popen_calls.append((args, kwargs))

    monkeypatch.setattr(InstallationConfig, "exists", classmethod(lambda cls: True))
    monkeypatch.setattr(restart_module.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        restart_module,
        "warn_before_voice_interruption",
        lambda **kwargs: warnings.append(kwargs),
    )
    # The machine's env file selects the coding backend, which gates
    # codex-app-server; pin it so the expected service set is deterministic.
    monkeypatch.setattr(
        restart_module,
        "configured_default_services",
        lambda: default_services("codex"),
    )

    result = CliRunner().invoke(restart, ["--delay", "0"])

    assert result.exit_code == 0
    assert "all Openbase-managed services" in result.output
    assert "Dispatcher context is preserved" in result.output
    assert "super-agents-mcp" not in result.output
    assert len(popen_calls) == 1

    command = popen_calls[0][0][0][2]
    assert "execute_restart_payload" in command
    assert "livekit-server" in command
    assert "codex-app-server" in command
    assert "django-cli" in command
    assert "codex-thread-device-sync" not in command
    assert "super-agents-mcp" not in command
    assert warnings == [{"reason": "restart", "emit_cli_warning": True}]


def test_self_restart_schedules_all_openbase_services(monkeypatch):
    popen_calls = []

    class FakePopen:
        def __init__(self, *args, **kwargs):
            popen_calls.append((args, kwargs))

    monkeypatch.setattr(InstallationConfig, "exists", classmethod(lambda cls: True))
    monkeypatch.setattr(restart_module.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        restart_module,
        "warn_before_voice_interruption",
        lambda **_kwargs: None,
    )

    result = CliRunner().invoke(self_restart, ["--delay", "0"])

    assert result.exit_code == 0
    assert "Scheduled self-restart" in result.output
    command = popen_calls[0][0][0][2]
    assert "livekit-server" in command
    assert "django-cli" in command


def test_restart_single_service_schedules_only_that_service(monkeypatch):
    popen_calls = []
    warnings = []

    class FakePopen:
        def __init__(self, *args, **kwargs):
            popen_calls.append((args, kwargs))

    monkeypatch.setattr(InstallationConfig, "exists", classmethod(lambda cls: True))
    monkeypatch.setattr(restart_module.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        restart_module,
        "warn_before_voice_interruption",
        lambda **kwargs: warnings.append(kwargs),
    )

    result = CliRunner().invoke(restart, ["--service", "codex-thread-sync"])

    assert result.exit_code == 0
    assert "codex-thread-sync" in result.output
    command = popen_calls[0][0][0][2]
    assert "codex-thread-sync" in command
    assert "livekit-agent" not in command
    assert warnings == []


def test_restart_optional_device_sync_can_be_targeted_explicitly(monkeypatch):
    popen_calls = []

    class FakePopen:
        def __init__(self, *args, **kwargs):
            popen_calls.append((args, kwargs))

    monkeypatch.setattr(InstallationConfig, "exists", classmethod(lambda cls: True))
    monkeypatch.setattr(restart_module.subprocess, "Popen", FakePopen)

    result = CliRunner().invoke(restart, ["--service", "codex-thread-device-sync"])

    assert result.exit_code == 0
    assert "codex-thread-device-sync" in result.output
    command = popen_calls[0][0][0][2]
    assert "codex-thread-device-sync" in command
    assert "codex-thread-sync" not in command


def test_restart_codex_app_server_only_targets_codex_service():
    plan = build_restart_plan(RestartRequest(services=("codex-app-server",)))

    assert plan.services == ("codex-app-server",)


def test_restart_super_agents_mcp_is_not_a_valid_target():
    result = CliRunner().invoke(restart, ["--service", "super-agents-mcp"])

    assert result.exit_code != 0
    assert "Invalid value for '--service'" in result.output


def test_restart_plan_rejects_super_agents_mcp_target():
    try:
        build_restart_plan(RestartRequest(services=("super-agents-mcp",)))
    except Exception as exc:
        assert "Unknown restart target 'super-agents-mcp'" in str(exc)
    else:
        raise AssertionError("super-agents-mcp should not be restartable")


def test_recreate_dispatcher_adds_livekit_agent():
    plan = build_restart_plan(
        RestartRequest(
            services=("codex-thread-sync",),
            recreate_dispatcher=True,
        )
    )

    assert plan.services == ("codex-thread-sync", "livekit-agent")
    assert plan.recreate_dispatcher is True


def test_execute_recreate_dispatcher_warms_thread_after_services_start(monkeypatch):
    calls = []

    async def fake_warm_dispatcher(*, fresh=False):
        calls.append(f"warm:fresh={fresh}")
        return "dispatcher-1"

    monkeypatch.setattr(
        restart_module,
        "require_installation",
        lambda: InstallationConfig(
            workspace_path="/tmp/workspace", env_file="/tmp/.env"
        ),
    )
    monkeypatch.setattr(
        restart_module, "launchctl_status", lambda _svc: {"installed": True}
    )
    monkeypatch.setattr(
        restart_module,
        "launchctl_bootout",
        lambda svc: calls.append(f"stop:{svc.name}"),
    )
    monkeypatch.setattr(
        restart_module,
        "install_service",
        lambda _config, svc: calls.append(f"start:{svc.name}"),
    )
    monkeypatch.setattr(restart_module.time, "sleep", lambda _seconds: None)

    from openbase_coder_cli import livekit_voice_route

    monkeypatch.setattr(
        livekit_voice_route,
        "prepare_livekit_dispatcher_recreation",
        lambda: calls.append("prepare"),
    )
    monkeypatch.setattr(
        livekit_voice_route,
        "warm_livekit_dispatcher_thread",
        fake_warm_dispatcher,
    )

    execute_restart_plan(
        RestartPlan(
            services=("livekit-agent",),
            recreate_dispatcher=True,
            interrupts_voice=False,
            delay_seconds=0,
        )
    )

    assert calls == [
        "prepare",
        "stop:livekit-agent",
        "start:livekit-agent",
        "warm:fresh=True",
    ]
